"""CLI: python -m tradingbot {backtest,data-sync,paper-once,paper-start,paper-stop,paper-status,ws-smoke}"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tradingbot.alerts import send_alert
from tradingbot.backtest import (
    DISCLAIMER,
    compute_metrics,
    fold_metrics,
    format_report,
    simulate_d1,
    simulate_placeholder,
)
from tradingbot.config import Config, LiveTradingRefused, ensure_safe_mode, load_config
from tradingbot.data.rest import OkxRestFeed
from tradingbot.logutil import JsonlLogger
from tradingbot.orders import OrderManager
from tradingbot.paperlog import (
    closed_trade as log_closed_trade,
    make_day,
    make_init,
    write_day_idempotent,
    write_init_if_needed,
)
from tradingbot.portfolio import Portfolio
from tradingbot.risk import RiskManager
from tradingbot.strategy.d1_breakout import DISCLAIMER as D1_DISCLAIMER, D1BreakoutStrategy
from tradingbot.strategy.my_d1 import DISCLAIMER as MY_D1_DISCLAIMER, MyD1Strategy
from tradingbot.strategy.placeholder import PLACEHOLDER_DISCLAIMER, PlaceholderEmaAtrStrategy
from tradingbot.types import OrderIntent
from tradingbot.universe import inst_id, load_favorites, panel_from_candles


def _ts_iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _feed(cfg: Config) -> OkxRestFeed:
    return OkxRestFeed(
        cache_dir=cfg.data.cache_dir,
        rest_base=cfg.data.rest_base,
        timeout_sec=cfg.data.timeout_sec,
        crypto_data_dir=cfg.crypto_data_dir,
    )


def _holdout_note() -> str:
    p = Path("/workspace/crypto/holdout_lock.json")
    if not p.exists():
        return ""
    try:
        h = json.loads(p.read_text())
        start = h.get("holdout_start") or h.get("start")
        return f"Holdout lock (read-only): holdout_start={start}. This harness does not tune parameters."
    except Exception:
        return ""


def _run_placeholder_variant(name, strat, candles, cfg, n_folds, oos_ratio) -> str:
    closed, eq, eq_ts = simulate_placeholder(candles, cfg, strat)
    bars = [c for c in candles if c.confirm == 1]
    folds = fold_metrics(closed, eq, eq_ts, bars, n_folds, oos_ratio, cfg.allocated_capital)
    # 2x fee stress
    st_fee = cfg.fee_taker * 2.0
    st_slip = st_fee * cfg.slippage_mult
    st_c, st_eq, st_ts = simulate_placeholder(candles, cfg, strat, fee=st_fee, slippage=st_slip)
    st_folds = fold_metrics(st_c, st_eq, st_ts, bars, n_folds, oos_ratio, cfg.allocated_capital)
    return format_report(
        name,
        bars,
        closed,
        eq,
        eq_ts,
        cfg,
        folds,
        stress=(st_c, st_eq, st_folds),
        extra_note=PLACEHOLDER_DISCLAIMER,
    )


def _load_d1_panel(feed: OkxRestFeed, cfg: Config, *, days: int, update: bool):
    bases = load_favorites(cfg.universe_file)
    by = {}
    missing = []
    for b in bases:
        inst = inst_id(b)
        try:
            cs = feed.get_candles(inst, "1Dutc", days=days, update=update)
        except Exception:
            cs = feed.get_candles(inst, "1Dutc", days=days, update=False)
        if cs:
            by[b] = cs
        else:
            missing.append(b)
    return by, missing


def _run_d1(cfg: Config, feed: OkxRestFeed, *, days: int, update: bool, n_folds: int, oos_ratio: float) -> str:
    from tradingbot.universe import breadth_top20, monthly_top20_universe

    by, missing = _load_d1_panel(feed, cfg, days=days, update=update)
    if "BTC" not in {k.upper() for k in by} and "BTC" not in by:
        raise SystemExit("D1 backtest needs BTC 1Dutc candles (seed or download).")
    C, HI, LO, VQ, O = panel_from_candles(by)
    # drop rows that are all-NaN; keep union index
    plugin = D1BreakoutStrategy(allocated_capital=cfg.allocated_capital)
    plugin.prepare(C, high=HI, low=LO, quote_vol=VQ)
    state = {}
    w, meta = plugin.weights_and_trades(state=state)
    closed, eq, eq_ts = simulate_d1(w, C, cfg, cfg.fee_taker, cfg.slippage, meta)

    # Build synthetic "bars" from the BTC (or panel) index for fold splits
    from tradingbot.types import Candle

    bars = []
    btc_c = C["BTC"]
    for ts, px in btc_c.items():
        if px != px:  # na
            continue
        ms = int(ts.value // 1_000_000) if hasattr(ts, "value") else int(ts)
        bars.append(Candle(ts=ms, open=float(px), high=float(px), low=float(px), close=float(px), confirm=1))
    # align trades/equity to bars length — equity is on C.index including possible leading nan
    # fold on C index length of equity
    folds = fold_metrics(closed, eq, eq_ts, bars[: len(eq)], n_folds, oos_ratio, cfg.allocated_capital)
    st_fee = cfg.fee_taker * 2.0
    st_slip = st_fee * cfg.slippage_mult
    st_c, st_eq, st_ts = simulate_d1(w, C, cfg, st_fee, st_slip, meta)
    st_folds = fold_metrics(st_c, st_eq, st_ts, bars[: len(st_eq)], n_folds, oos_ratio, cfg.allocated_capital)
    miss = f"Missing 1Dutc cache for: {', '.join(missing)}" if missing else ""
    note = D1_DISCLAIMER + (f"\n{miss}" if miss else "")
    note += f"\nUniverse: {cfg.universe_file} ({len(by)}/{len(by)+len(missing)} names loaded). Breadth uses point-in-time monthly top-20."
    return format_report(
        "REF_d1_a4_soozie  1Dutc  favorites",
        bars[: len(eq)],
        closed,
        eq,
        eq_ts,
        cfg,
        folds,
        stress=(st_c, st_eq, st_folds),
        extra_note=note,
    )


def _run_my_d1(cfg: Config, feed: OkxRestFeed, *, days: int, update: bool, n_folds: int, oos_ratio: float) -> str:
    """Run the unlocked MyD1 strategy with tuned parameters (lookback=10)."""
    from tradingbot.universe import breadth_top20, monthly_top20_universe

    by, missing = _load_d1_panel(feed, cfg, days=days, update=update)
    if "BTC" not in {k.upper() for k in by} and "BTC" not in by:
        raise SystemExit("MyD1 backtest needs BTC 1Dutc candles (seed or download).")
    C, HI, LO, VQ, O = panel_from_candles(by)

    # Tuned parameters from sweep: lookback=10 passed safety bar
    plugin = MyD1Strategy(
        allocated_capital=cfg.allocated_capital,
        lookback=20,
        breadth_min=0.5,
        risk_pct=0.01,
        max_weight=0.10,
        exposure_scale=0.50,
    )
    plugin.prepare(C, high=HI, low=LO, quote_vol=VQ)
    state = {}
    w, meta = plugin.weights_and_trades(state=state)
    closed, eq, eq_ts = simulate_d1(w, C, cfg, cfg.fee_taker, cfg.slippage, meta)

    # Build synthetic "bars" from the BTC index for fold splits
    from tradingbot.types import Candle

    bars = []
    btc_c = C["BTC"]
    for ts, px in btc_c.items():
        if px != px:  # na
            continue
        ms = int(ts.value // 1_000_000) if hasattr(ts, "value") else int(ts)
        bars.append(Candle(ts=ms, open=float(px), high=float(px), low=float(px), close=float(px), confirm=1))
    folds = fold_metrics(closed, eq, eq_ts, bars[: len(eq)], n_folds, oos_ratio, cfg.allocated_capital)
    st_fee = cfg.fee_taker * 2.0
    st_slip = st_fee * cfg.slippage_mult
    st_c, st_eq, st_ts = simulate_d1(w, C, cfg, st_fee, st_slip, meta)
    st_folds = fold_metrics(st_c, st_eq, st_ts, bars[: len(st_eq)], n_folds, oos_ratio, cfg.allocated_capital)
    miss = f"Missing 1Dutc cache for: {', '.join(missing)}" if missing else ""
    note = MY_D1_DISCLAIMER + (f"\n{miss}" if miss else "")
    note += "\nParameters: lookback=20, exposure_scale=0.50, breadth_min=0.5, risk_pct=0.01, max_weight=0.10"
    note += f"\nUniverse: {cfg.universe_file} ({len(by)}/{len(by)+len(missing)} names loaded). Breadth uses point-in-time monthly top-20."
    return format_report(
        "MY_D1_breakout  1Dutc  favorites  lookback=20 scale=0.50",
        bars[: len(eq)],
        closed,
        eq,
        eq_ts,
        cfg,
        folds,
        stress=(st_c, st_eq, st_folds),
        extra_note=note,
    )


def cmd_backtest(cfg: Config, args) -> int:
    names = [s.strip() for s in (args.strategies or "placeholder,d1").split(",") if s.strip()]
    if len(names) > 3:
        print("Harness supports at most 3 strategy variants side by side.", file=sys.stderr)
        return 2
    unknown = [n for n in names if n not in ("placeholder", "placeholder_slow", "d1", "my_d1")]
    if unknown:
        print(f"Unknown strategy: {unknown}. Use placeholder, placeholder_slow, d1.", file=sys.stderr)
        return 2
    feed = _feed(cfg)
    symbol = args.symbol or cfg.backtest.symbol or cfg.symbol
    bar = args.bar or cfg.backtest.bar or "4H"
    days = args.days if args.days is not None else cfg.backtest.days
    update = not args.no_update
    n_folds = args.folds or cfg.backtest.n_folds
    oos_ratio = cfg.backtest.oos_ratio
    # mutate a copy-like cfg for the chosen symbol/bar (config object is fine to tweak locally)
    cfg.symbol = symbol
    cfg.bar = bar
    from tradingbot.termfmt import kv, title

    print(title("Backtest", DISCLAIMER))
    hold = _holdout_note()
    if hold:
        print(hold)
    print(kv([
        ("Mode", "dry_run/paper only"),
        ("Capital", f"{cfg.allocated_capital:.0f} USDT"),
        ("Variants", ", ".join(names)),
    ]))
    reports = []
    if "placeholder" in names or "placeholder_slow" in names:
        try:
            candles = feed.get_candles(symbol, bar, days=days, update=update)
        except Exception as e:
            print(f"Update failed ({e}); retrying cache/seed only.")
            candles = feed.get_candles(symbol, bar, days=days, update=False)
        if not candles:
            print(f"No {symbol} {bar} candles available.", file=sys.stderr)
            return 1
        if "placeholder" in names:
            reports.append(
                _run_placeholder_variant(
                    f"PLACEHOLDER_ema_cross_atr  {symbol} {bar}",
                    PlaceholderEmaAtrStrategy(),
                    candles,
                    cfg,
                    n_folds,
                    oos_ratio,
                )
            )
        if "placeholder_slow" in names:
            reports.append(
                _run_placeholder_variant(
                    f"PLACEHOLDER_ema_cross_atr(20/50)  {symbol} {bar}",
                    PlaceholderEmaAtrStrategy(fast=20, slow=50),
                    candles,
                    cfg,
                    n_folds,
                    oos_ratio,
                )
            )
    if "d1" in names:
        d1_days = max(days, 800) if bar in ("1H", "4H") else days
        try:
            reports.append(_run_d1(cfg, feed, days=d1_days, update=update, n_folds=n_folds, oos_ratio=oos_ratio))
        except SystemExit:
            raise
        except Exception as e:
            print(f"D1 backtest failed: {e}", file=sys.stderr)
            raise
    if "my_d1" in names:
        my_d1_days = max(days, 800) if bar in ("1H", "4H") else days
        try:
            reports.append(_run_my_d1(cfg, feed, days=my_d1_days, update=update, n_folds=n_folds, oos_ratio=oos_ratio))
        except SystemExit:
            raise
        except Exception as e:
            print(f"MyD1 backtest failed: {e}", file=sys.stderr)
            raise
    print("\n\n".join(reports))
    print("\nDone. These are placeholder/reference-plugin results, not a performance claim.")
    return 0


def run_paper_iteration(cfg: Config, *, strategy: str = "d1", update: bool = True, bar: str | None = None, run_id: str | None = None) -> tuple[int, dict]:
    """One paper cycle on the last closed bar. Shared by paper-once and the 24h loop."""
    log = JsonlLogger(cfg.log_path)
    name = strategy or "d1"
    feed = _feed(cfg)
    # local alias so the rest of the body can use args.bar / args.no_update no longer
    book = Portfolio.load(cfg.state_path, allocated_capital=cfg.allocated_capital)
    risk = RiskManager(cfg.risk, cfg.allocated_capital)
    broker = OrderManager(cfg.fee_taker, cfg.slippage)
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    marks = {}
    closed_for_log = []
    d1_closed = []
    sleeve_weights = {}
    positions_view = {}
    s1_stops = {}
    book_note = ""

    if name in ("placeholder", "placeholder_slow"):
        strat = PlaceholderEmaAtrStrategy() if name == "placeholder" else PlaceholderEmaAtrStrategy(20, 50)
        candles = feed.get_candles(cfg.symbol, bar or cfg.bar or "4H", days=400, update=update)
        if not candles:
            print("No candles.", file=sys.stderr)
            return 1, {"message": "no candles", "strategy": name}
        last = candles[-1]
        marks[cfg.symbol] = last.close
        eq = book.equity(marks)
        book.update_peaks(eq, day=_ts_iso_utc(last.ts))
        sig = strat.on_candles(cfg.symbol, candles, book.positions.get(cfg.symbol))
        log.info("paper_once_signal", strategy=strat.name, signal=None if not sig else sig.reason)
        if sig:
            px = last.close
            dec = risk.evaluate(sig, book, px, marks=marks)
            if dec.allowed:
                intent = OrderIntent(
                    inst_id=sig.inst_id,
                    side="buy" if sig.side == "buy" else "sell",
                    qty=dec.qty,
                    price=px,
                    ts=sig.ts,
                    signal_id=sig.signal_id,
                    client_oid=dec.client_oid,
                    stop=sig.stop_price,
                    is_exit=sig.side in ("close", "sell"),
                    initial_risk=dec.initial_risk,
                    reason=sig.reason,
                )
                fill = broker.submit_paper(book, intent)
                log.info("paper_fill", fill=fill)
                if fill.is_exit:
                    closed_for_log.append(
                        log_closed_trade(
                            sleeve=strat.name,
                            coin=cfg.symbol.replace("-USDT", ""),
                            entry=0.0,
                            exit=fill.price,
                            fee_side=cfg.fee_taker,
                            slip_side=cfg.slippage,
                        )
                    )
            else:
                log.info("paper_rejected", reason=dec.reason)
        pos = book.positions.get(cfg.symbol)
        if pos:
            positions_view = {"placeholder": {cfg.symbol.replace("-USDT", ""): pos.qty * pos.avg_px / max(book.equity(marks), 1e-9)}}
            s1_stops = {cfg.symbol.replace("-USDT", ""): {"stop": pos.stop, "entry": pos.avg_px}}
            sleeve_weights = {"placeholder": positions_view["placeholder"] and 1.0 or 0.0}
        ready = _ts_iso_utc(last.ts)
        strat_name = strat.name
    elif name == "d1":
        by, missing = _load_d1_panel(feed, cfg, days=1800, update=update)
        C, HI, LO, VQ, O = panel_from_candles(by)
        plugin = D1BreakoutStrategy(allocated_capital=cfg.allocated_capital)
        plugin.prepare(C, high=HI, low=LO, quote_vol=VQ)
        state = {}
        w, meta = plugin.weights_and_trades(state=state)
        last_ts = C.index[-1]
        ready = str(getattr(last_ts, "date")())
        last_w = w.iloc[-1]
        sleeve_weights = {"D1": float(last_w.sum())}
        positions_view = {"D1": {s: float(last_w[s]) for s in last_w.index if last_w[s] > 0}}
        s1_stops = {s: {"stop": state[s]["stop"], "entry": state[s]["entry"]} for s in state}
        # Sync book to target weights at last close (paper)
        marks = {inst_id(s): float(C[s].iloc[-1]) for s in C.columns if C[s].notna().iloc[-1]}
        eq0 = book.equity(marks)
        book.update_peaks(eq0, day=ready)
        # exits first
        for inst, pos in list(book.positions.items()):
            base = inst.replace("-USDT", "")
            target = float(last_w[base]) if base in last_w.index else 0.0
            if target <= 0 and pos.qty > 0:
                px = marks.get(inst, pos.avg_px)
                sig_id = f"{plugin.name}:{inst}:{int(pd_ts_ms(last_ts))}:close"
                from tradingbot.types import Signal
                sig = Signal(inst_id=inst, side="close", ts=int(pd_ts_ms(last_ts)), signal_id=sig_id, strategy=plugin.name, reason="d1 paper-once flatten")
                dec = risk.evaluate(sig, book, px, marks=marks)
                if dec.allowed:
                    fill = broker.submit_paper(
                        book,
                        OrderIntent(
                            inst_id=inst, side="sell", qty=dec.qty, price=px, ts=sig.ts,
                            signal_id=sig_id, client_oid=dec.client_oid, is_exit=True, reason=sig.reason,
                        ),
                    )
                    d1_closed.append(log_closed_trade(sleeve="D1", coin=base, entry=pos.avg_px, exit=fill.price, fee_side=cfg.fee_taker, slip_side=cfg.slippage))
        # entries
        from tradingbot.types import Signal
        for s, wt in last_w.items():
            if wt <= 0:
                continue
            inst = inst_id(s)
            if inst in book.positions:
                continue
            if s not in state:
                continue
            px = marks.get(inst)
            if not px:
                continue
            sig = Signal(
                inst_id=inst, side="buy", ts=int(pd_ts_ms(last_ts)),
                signal_id=f"{plugin.name}:{inst}:{int(pd_ts_ms(last_ts))}:buy",
                stop_price=state[s]["stop"], reason="d1 paper-once entry", strategy=plugin.name, strength=float(wt),
            )
            dec = risk.evaluate(sig, book, px, marks=marks, weight_hint=float(wt))
            if dec.allowed:
                broker.submit_paper(
                    book,
                    OrderIntent(
                        inst_id=inst, side="buy", qty=dec.qty, price=px, ts=sig.ts,
                        signal_id=sig.signal_id, client_oid=dec.client_oid, stop=sig.stop_price,
                        initial_risk=dec.initial_risk, reason=sig.reason,
                    ),
                )
        if missing:
            log.warning("d1_missing_symbols", symbols=missing)
        strat_name = plugin.name
        day = ready
    elif name == "my_d1":
        by, missing = _load_d1_panel(feed, cfg, days=1800, update=update)
        C, HI, LO, VQ, O = panel_from_candles(by)
        plugin = MyD1Strategy(
            allocated_capital=cfg.allocated_capital,
            lookback=20,
            breadth_min=0.5,
            risk_pct=0.01,
            max_weight=0.10,
            exposure_scale=0.50,
        )
        plugin.prepare(C, high=HI, low=LO, quote_vol=VQ)
        state = {}
        w, meta = plugin.weights_and_trades(state=state)
        last_ts = C.index[-1]
        ready = str(getattr(last_ts, "date")())
        last_w = w.iloc[-1]
        sleeve_weights = {"MyD1": float(last_w.sum())}
        positions_view = {"MyD1": {s: float(last_w[s]) for s in last_w.index if last_w[s] > 0}}
        s1_stops = {s: {"stop": state[s]["stop"], "entry": state[s]["entry"]} for s in state}
        # Sync book to target weights at last close (paper)
        marks = {inst_id(s): float(C[s].iloc[-1]) for s in C.columns if C[s].notna().iloc[-1]}
        eq0 = book.equity(marks)
        book.update_peaks(eq0, day=ready)
        # exits first
        for inst, pos in list(book.positions.items()):
            base = inst.replace("-USDT", "")
            target = float(last_w[base]) if base in last_w.index else 0.0
            if target <= 0 and pos.qty > 0:
                px = marks.get(inst, pos.avg_px)
                sig_id = f"{plugin.name}:{inst}:{int(pd_ts_ms(last_ts))}:close"
                from tradingbot.types import Signal
                sig = Signal(inst_id=inst, side="close", ts=int(pd_ts_ms(last_ts)), signal_id=sig_id, strategy=plugin.name, reason="my_d1 paper-once flatten")
                dec = risk.evaluate(sig, book, px, marks=marks)
                if dec.allowed:
                    fill = broker.submit_paper(
                        book,
                        OrderIntent(
                            inst_id=inst, side="sell", qty=dec.qty, price=px, ts=sig.ts,
                            signal_id=sig_id, client_oid=dec.client_oid, is_exit=True, reason=sig.reason,
                        ),
                    )
                    d1_closed.append(log_closed_trade(sleeve="MyD1", coin=base, entry=pos.avg_px, exit=fill.price, fee_side=cfg.fee_taker, slip_side=cfg.slippage))
        # entries
        from tradingbot.types import Signal
        for s, wt in last_w.items():
            if wt <= 0:
                continue
            inst = inst_id(s)
            if inst in book.positions:
                continue
            if s not in state:
                continue
            px = marks.get(inst)
            if not px:
                continue
            sig = Signal(
                inst_id=inst, side="buy", ts=int(pd_ts_ms(last_ts)),
                signal_id=f"{plugin.name}:{inst}:{int(pd_ts_ms(last_ts))}:buy",
                stop_price=state[s]["stop"], reason="my_d1 paper-once entry", strategy=plugin.name, strength=float(wt),
            )
            dec = risk.evaluate(sig, book, px, marks=marks, weight_hint=float(wt))
            if dec.allowed:
                broker.submit_paper(
                    book,
                    OrderIntent(
                        inst_id=inst, side="buy", qty=dec.qty, price=px, ts=sig.ts,
                        signal_id=sig.signal_id, client_oid=dec.client_oid, stop=sig.stop_price,
                        initial_risk=dec.initial_risk, reason=sig.reason,
                    ),
                )
        if missing:
            log.warning("my_d1_missing_symbols", symbols=missing)
        strat_name = plugin.name
        day = ready
        if float(last_w.sum()) <= 0:
            book_note = "flat: no 20-day breakout open on the last closed daily bar."
        else:
            book_note = f"open names={int((last_w > 0).sum())} gross={float(last_w.sum()):.3f}"
    elif name in ("v2", "v2-core", "v2-official"):
        from tradingbot.eval.v2_paper import run_v2_iteration
        from tradingbot.strategies.spec_v2_common import OFFICIAL_RUN_ID
        rid = run_id or OFFICIAL_RUN_ID
        return run_v2_iteration(cfg, run_id=rid, official=(rid == OFFICIAL_RUN_ID), update=update)
    elif name in ("v3", "v3-official", "h5", "h1", "h4") or (run_id or "").startswith("spec_v3"):
        from tradingbot.eval.v3_paper import OFFICIAL_RUN_ID as V3_RUN_ID
        from tradingbot.eval.v3_paper import run_v3_iteration
        rid = run_id or V3_RUN_ID
        return run_v3_iteration(cfg, run_id=rid, official=(rid == V3_RUN_ID), update=update)
    else:
        print(f"Unknown strategy {name}", file=sys.stderr)
        return 2, {"message": f"unknown strategy {name}", "strategy": name}

    book.save(cfg.state_path)
    cap = cfg.allocated_capital
    eq = book.equity(marks)
    write_init_if_needed(
        cfg.paper_log_path,
        make_init(
            date=day,
            allocated_capital=cap,
            sleeve_weights=sleeve_weights,
            positions=positions_view,
            s1_stops=s1_stops,
            state_as_of_close=day,
        ),
    )
    write_day_idempotent(
        cfg.paper_log_path,
        make_day(
            date=day,
            allocated_capital=cap,
            sleeve_weights=sleeve_weights,
            positions=positions_view,
            book_ret=0.0,
            d1_ret=0.0,
            book_equity=eq,
            d1_equity=eq if name == "d1" else cap,
            book_trades_closed=closed_for_log,
            d1_trades_closed=d1_closed,
        ),
        ready_date=day,
    )
    send_alert(f"paper-once {name} equity={eq:.2f} USDT (allocated_capital={cap})", logger=log)
    msg = (
        f"paper-once {name}: equity={eq:.2f} USDT  cash={book.cash:.2f}  "
        f"positions={list(book.positions)}  halted={book.halted}"
        + (f"  {book_note}" if book_note else "")
    )
    summary = {
        "message": msg,
        "strategy": name,
        "equity": eq,
        "cash": book.cash,
        "positions": list(book.positions),
        "halted": book.halted,
        "state_path": cfg.state_path,
        "paper_log_path": cfg.paper_log_path,
        "allocated_capital": cap,
        "ready": day,
    }
    return 0, summary


def cmd_paper_once(cfg: Config, args) -> int:
    rc, summary = run_paper_iteration(
        cfg,
        strategy=args.strategy or "d1",
        update=not args.no_update,
        bar=getattr(args, "bar", None),
    )
    if rc == 0:
        from tradingbot.termfmt import kv, px, title

        positions = summary.get("positions") or []
        print(title("Paper once"))
        print(kv([
            ("Strategy", str(summary.get("strategy") or "-")),
            ("Equity", px(float(summary.get("equity") or 0))),
            ("Cash", px(float(summary.get("cash") or 0))),
            ("Positions", ", ".join(positions) if positions else "none"),
            ("Halted", str(summary.get("halted"))),
            ("Note", summary.get("message") or "-"),
            ("State", str(summary.get("state_path") or "-")),
            ("Log", str(summary.get("paper_log_path") or "-")),
        ]))
        print("Paper / dry-run only. allocated_capital is not the OKX main balance.")
    return rc


def pd_ts_ms(ts) -> int:
    import pandas as pd
    return int(pd.Timestamp(ts).value // 1_000_000)



def cmd_data_sync(cfg: Config, args) -> int:
    from tradingbot.data.pipeline import sync_universe
    from tradingbot.data.store import cache_backend

    bars = [b.strip() for b in (args.bars or "4H,1Dutc").split(",") if b.strip()]
    include_1h = bool(args.include_1h) or ("1H" in bars)
    from tradingbot.termfmt import kv, title

    print(title("Data sync", "Public OKX REST only. No account balance."))
    print(kv([
        ("Universe", cfg.universe_file),
        ("Bars", ",".join(bars)),
        ("Include 1H", "yes" if include_1h else "no"),
        ("Cache", cache_backend()),
        ("Cache dir", cfg.data.cache_dir),
        ("Update", "yes" if not args.no_update else "no"),
    ]))
    result = sync_universe(
        cfg,
        bars=bars,
        days=args.days,
        update=not args.no_update,
        include_1h=include_1h,
        report_dir=args.report_dir or "data",
        progress=True,
    )
    counts = [s["n_closed"] for s in result.series]
    issues = [s for s in result.series if not s.get("ok")]
    done = [
        ("Symbols", str(len(result.symbols))),
        ("Series", str(len(result.series))),
        ("Format", result.backend),
        ("Issues", str(len(issues))),
        ("Fetch errors", str(len(result.errors))),
    ]
    if counts:
        done.append(("Closed bars", f"min {min(counts)}  mid {sorted(counts)[len(counts)//2]}  max {max(counts)}"))
    done.append(("Report", str(result.report_md)))
    print(title("Data sync done"))
    print(kv(done))
    if issues:
        print(title("Issues"))
        for s in issues:
            print(f"  {s['symbol']} {s['bar']}: {', '.join(s['issues'])}")
    return 1 if result.errors else 0


def cmd_ws_smoke(cfg: Config, args) -> int:
    from tradingbot.data.ws import OkxPublicWS

    client = OkxPublicWS(url=cfg.data.ws_public)
    inst = args.symbol or cfg.symbol
    bar = args.bar or "1H"
    try:
        msg = client.run_smoke(inst_id=inst, bar=bar, timeout=args.timeout)
    except Exception as e:
        print(f"WS smoke failed: {e}", file=sys.stderr)
        return 1
    # do not dump huge payloads; show keys only
    arg = msg.get("arg") or {}
    n = len(msg.get("data") or [])
    from tradingbot.termfmt import kv, title

    print(title("WS smoke"))
    print(kv([
        ("Status", "ok"),
        ("Channel", str(arg.get("channel") or "-")),
        ("Inst", str(arg.get("instId") or "-")),
        ("Rows", str(n)),
    ]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tradingbot", description="OKX spot paper/dry-run engine (T1 scaffold)")
    p.add_argument("--config", default=None, help="YAML config path")
    p.add_argument("--live", action="store_true", help="Required (with LIVE_APPROVED=1 and mode=live) for live — still refused")
    sub = p.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest", help="Walk-forward backtest (placeholder / D1 reference, up to 3 variants)")
    bt.add_argument("--symbol", default=None)
    bt.add_argument("--bar", default="4H", help="Primary bar is 4H (or 1Dutc). 1H is optional/secondary.")
    bt.add_argument("--days", type=int, default=None)
    bt.add_argument("--no-update", action="store_true")
    bt.add_argument("--strategies", default="placeholder,d1", help="Comma list, max 3: placeholder,placeholder_slow,d1,my_d1")
    bt.add_argument("--folds", type=int, default=None)
    po = sub.add_parser("paper-once", help="One paper cycle on the last closed bar")
    po.add_argument("--strategy", default="d1", choices=["d1", "placeholder", "placeholder_slow", "my_d1"])
    po.add_argument("--bar", default=None)
    po.add_argument("--no-update", action="store_true")
    ps = sub.add_parser("paper-start", help="Start 24h paper loop in background (pidfile). Does not place live orders.")
    ps.add_argument("--strategies", default="d1", help="Comma list, or 'v2' / 'v3' for official paper clocks")
    ps.add_argument("--poll", type=int, default=60, help="Seconds between polls (default 60)")
    ps.add_argument("--run-id", default=None, help="Paper run id (spec_v3_official or spec_v2_official)")
    pstop = sub.add_parser("paper-stop", help="Stop the background paper loop")
    pstop.add_argument("--run-id", default=None)
    pstat = sub.add_parser("paper-status", help="Show paper loop status")
    pstat.add_argument("--run-id", default=None)
    ploop = sub.add_parser("paper-loop", help=argparse.SUPPRESS)  # foreground loop, used by paper-start
    ploop.add_argument("--strategies", default="d1")
    ploop.add_argument("--poll", type=int, default=60)
    ploop.add_argument("--run-id", default=None)
    ds = sub.add_parser("data-sync", help="Incremental historical download (4H+1Dutc; 1H optional) + quality report")
    ds.add_argument("--bars", default="4H,1Dutc", help="Comma list. Default 4H,1Dutc (1H optional).")
    ds.add_argument("--include-1h", action="store_true", help="Also sync 1H (optional/secondary).")
    ds.add_argument("--days", type=int, default=None, help="History window in days (default 1800 / 400 for 1H).")
    ds.add_argument("--no-update", action="store_true", help="Rebuild cache/report from seed/cache only, no REST.")
    ds.add_argument("--report-dir", default="data")
    sm = sub.add_parser("summary", help="Fast live watchlist tables. No orders.")
    sm.add_argument("--top", type=int, default=8)
    sm.add_argument("--symbol", default=None, help="One coin, e.g. HYPE")
    sig = sub.add_parser("signals", help="Side feature: paper levels. No orders. No strategy take-profit.")
    sig.add_argument("--symbol", default=None, help="One coin, e.g. HYPE")
    sig.add_argument("--top", type=int, default=10)
    sig.add_argument("--days", type=int, default=800)
    sig.add_argument("--no-update", action="store_true", help="Cache only. Default pulls OKX public REST first.")
    ws = sub.add_parser("ws-smoke", help="Short public WS connect (candles/tickers)")
    ws.add_argument("--symbol", default=None)
    ws.add_argument("--bar", default="1H")
    ws.add_argument("--timeout", type=float, default=12.0)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    try:
        ensure_safe_mode(cfg, live_flag=bool(args.live))
    except LiveTradingRefused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    except NotImplementedError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    if args.cmd == "backtest":
        return cmd_backtest(cfg, args)
    if args.cmd == "paper-once":
        return cmd_paper_once(cfg, args)
    if args.cmd == "paper-start":
        from tradingbot.strategies.spec_v2_common import OFFICIAL_RUN_ID
        from tradingbot.eval.v3_paper import OFFICIAL_RUN_ID as V3_RUN_ID
        run_id = args.run_id
        strat = str(getattr(args, "strategies", "") or "")
        if run_id is None and strat.startswith("v3"):
            run_id = V3_RUN_ID
        elif run_id is None and strat.startswith("v2"):
            run_id = OFFICIAL_RUN_ID
        from tradingbot.paper_runner import start_background
        pid, msg = start_background(
            cfg,
            strategies=args.strategies,
            poll_sec=args.poll,
            config_path=getattr(args, "config", None),
            run_id=run_id,
        )
        from tradingbot.termfmt import title

        print(title("Paper start"))
        print(msg)
        if str(msg).startswith("REFUSED"):
            return 2
        if (run_id or "") == V3_RUN_ID or strat.startswith("v3"):
            print("Paper / dry-run only. No keys. No live. C_paper=10000. Not a strategy PASS.")
        else:
            print("Paper / dry-run only. No keys. No live.")
        return 0
    if args.cmd == "paper-stop":
        from tradingbot.paper_runner import stop_background
        from tradingbot.termfmt import title

        print(title("Paper stop"))
        print(stop_background(cfg, run_id=getattr(args, "run_id", None)))
        return 0
    if args.cmd == "paper-status":
        from tradingbot.paper_runner import format_status, get_status
        run_id = getattr(args, "run_id", None)
        print(format_status(get_status(cfg, run_id=run_id)))
        status_md = Path("/workspace/sprint/artifacts/spec_v3/paper_status.md")
        if (run_id or "").startswith("spec_v3") and status_md.exists():
            print()
            print(status_md.read_text())
        return 0
    if args.cmd == "paper-loop":
        from tradingbot.paper_runner import run_loop
        return run_loop(cfg, strategies=args.strategies, poll_sec=args.poll, run_id=getattr(args, "run_id", None))
    if args.cmd == "data-sync":
        return cmd_data_sync(cfg, args)
    if args.cmd == "summary":
        from tradingbot.summary_view import print_summary
        return print_summary(cfg, top=args.top, symbol=args.symbol)
    if args.cmd == "signals":
        from tradingbot.side_signals import print_signals
        return print_signals(
            cfg,
            symbol=args.symbol,
            top=args.top,
            days=args.days,
            update=not args.no_update,
        )
    if args.cmd == "ws-smoke":
        return cmd_ws_smoke(cfg, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
