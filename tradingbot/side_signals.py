"""Side feature: paper levels for a coin, or the nearest setups.

Not the strategy exit path. Does not place orders.
Strategy exit is still the ratchet stop or the BTC gate. A 2R number is a label only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tradingbot.cli import _feed, _load_d1_panel
from tradingbot.config import Config
from tradingbot.strategy.my_d1 import MyD1Strategy
from tradingbot.termfmt import bullets, kv, pct, px, table, title
from tradingbot.universe import inst_id, panel_from_candles


def print_signals(
    cfg: Config,
    *,
    symbol: str | None = None,
    top: int = 10,
    days: int = 800,
    update: bool = True,
) -> int:
    feed = _feed(cfg)
    source = "OKX public REST candles + live tickers" if update else "cached candles + live OKX tickers"
    print(title("OKX spot paper signals", "Not a live order. Live price above a level is not a buy until the daily bar closes."))
    by, missing = _load_d1_panel(feed, cfg, days=days, update=update)
    print(kv([("Data", source)]))
    C, HI, LO, VQ, _O = panel_from_candles(by)
    plugin = MyD1Strategy(
        cfg.allocated_capital,
        lookback=8,
        breadth_min=0.7,
        risk_pct=0.03,
        max_weight=0.05,
        exposure_scale=1.00,
    )
    plugin.prepare(C, high=HI, low=LO, quote_vol=VQ)
    state: dict = {}
    w, _trades = plugin.weights_and_trades(state=state)
    last = w.iloc[-1]
    held = last[last > 0].sort_values(ascending=False)

    btc = float(C["BTC"].iloc[-1])
    sma200 = float(C["BTC"].rolling(200).mean().iloc[-1])
    gate_on = btc > sma200
    tickers = feed.get_spot_tickers()
    live = tickers.get("BTC-USDT")
    print("Not a live order. Strategy has no take-profit. TP 2R is a label only.")
    print(kv([
        ("Last closed bar", str(C.index[-1])),
        ("BTC live", px(live.last) if live is not None else "n/a"),
        ("BTC close", px(btc)),
        ("SMA200", px(sma200)),
        ("Gate", "ON" if gate_on else "OFF"),
        ("Open breakouts", str(len(held))),
        ("Gross weight", f"{float(last.sum()):.3f}"),
    ]))

    if held.empty:
        print(title("Buy now", "None."))
    else:
        book_rows = []
        for coin, wt in held.head(top).items():
            st = state.get(coin, {})
            entry = float(st.get("entry") or 0.0)
            stop = float(st.get("stop") or 0.0)
            last_px = float(C[coin].iloc[-1])
            risk = entry - stop
            tp = entry + 2.0 * risk if risk > 0 else float("nan")
            book_rows.append([coin, f"{wt:.3f}", px(entry), px(stop), px(tp), px(last_px)])
        print(title("In book"))
        print(table(["Coin", "Weight", "Entry", "SL", "TP 2R", "Last"], book_rows, right={1, 2, 3, 4, 5}))

    prev_high = C.shift(1).rolling(20).max()
    mid = (C.rolling(20).max() + C.rolling(20).min()) / 2.0
    close = C.iloc[-1]
    level = prev_high.iloc[-1]
    live_px = {}
    for coin in C.columns:
        tick = tickers.get(inst_id(coin))
        if tick is not None:
            live_px[coin] = tick.last
    if live_px:
        print(title("Live prices", f"{len(live_px)} OKX tickers. A live price above the level is not a buy until the daily bar closes."))
    else:
        print(title("Live prices", "Tickers unavailable. Gaps use the last closed close."))
    rows = []
    for coin in C.columns:
        if coin not in level.index or level[coin] != level[coin]:
            continue
        px_live = live_px.get(coin)
        px_closed = float(close[coin]) if close[coin] == close[coin] else None
        price = px_live if px_live is not None else px_closed
        if price is None or price <= 0:
            continue
        brk = float(level[coin])
        rows.append((brk / price - 1.0, coin, price, brk, px_closed))
    rows.sort()
    nearest = [r for r in rows if r[0] > 0][:top]
    already = [r for r in rows if r[0] <= 0][:top]
    print(title("Closest, still under the level", "Not buys. Daily close has to confirm."))
    print(table(
        ["Coin", "Live", "Needs above", "Gap"],
        [[coin, px(price), px(brk), pct(g)] for g, coin, price, brk, _closed in nearest],
        right={1, 2, 3},
    ))
    print(title("Live above the old high", "Still not a buy until the daily bar closes."))
    print(table(
        ["Coin", "Live", "Level", "Above"],
        [[coin, px(price), px(brk), pct(-g)] for g, coin, price, brk, _closed in already],
        right={1, 2, 3},
    ))
    _write_report(C.index[-1], btc, sma200, gate_on, held, state, last, nearest, already, C)

    if symbol:
        raw = symbol.upper().replace("-USDT", "")
        if raw not in C.columns:
            print(title(raw))
            print("Not in the loaded universe.")
            return 1
        px_close = float(C[raw].iloc[-1])
        brk = float(prev_high[raw].iloc[-1])
        stop_now = float(mid[raw].iloc[-1])
        live_last = live_px.get(raw)
        one = [("Last closed close", px(px_close))]
        if live_last is not None:
            one.append(("Live", px(live_last)))
            one.append(("Gap to breakout", pct(brk / live_last - 1)))
        one.append(("Breakout level", px(brk)))
        if raw in state:
            st = state[raw]
            entry = float(st["entry"])
            stop = float(st["stop"])
            risk = entry - stop
            tp = entry + 2.0 * risk if risk > 0 else float("nan")
            one.extend([
                ("Status", "in book"),
                ("Entry", px(entry)),
                ("SL", px(stop)),
                ("TP 2R", px(tp)),
                ("Weight", f"{float(last[raw]):.3f}"),
            ])
        else:
            one.append(("Status", "no entry"))
            if px_close < brk:
                one.append(("Why", f"close is {pct(brk / px_close - 1)} under the breakout level"))
            else:
                one.append(("Why", "level met, but top-20, breadth, or stop filter failed"))
            if stop_now >= px_close:
                one.append(("Stop", f"none. 20-day mid {px(stop_now)} is above the close"))
            else:
                one.append(("Hypothetical SL", px(stop_now)))
                one.append(("Hypothetical TP 2R", px(px_close + 2.0 * (px_close - stop_now))))
                one.append(("Note", "label only, not a signal"))
        print(title(raw))
        print(kv(one))

    # CONCLUSION: what the numbers actually say
    concl: list[str] = []
    if not gate_on:
        concl.append("BTC under SMA200. Gate OFF. Strategy is flat or exiting; cash is the position.")
    elif float(last.sum()) <= 0:
        concl.append("Gate ON but no breakout open. The bot only buys a daily close above a 20-day high.")
    else:
        concl.append(f"Gate ON. Book holds {len(held)} breakout(s), gross {float(last.sum()):.2f}x of the 50%-capped sleeve.")
    if nearest:
        g0, coin0, _p, brk0, _c = nearest[0]
        concl.append(f"Closest not-yet signal: {coin0}, {pct(g0)} under its level ({px(brk0)}).")
    if already:
        names = ", ".join(c for _g, c, *_ in already[:3])
        concl.append(f"Above the level but unconfirmed: {names}. No buy until the daily bar closes.")
    concl.append("Strategy has no take-profit. Exit is the mid-channel ratchet stop, or BTC back under SMA200. TP 2R is a label only.")
    concl.append("Verdict: paper only. No live order, and the OKX wedding-funds balance stays untouched.")
    print(title("Conclusion"))
    print(bullets(concl))
    if missing:
        print(f"missing cache: {len(missing)}")
    print("report -> logs/signals_latest.md")
    return 0


def _write_report(as_of, btc, sma200, gate_on, held, state, last, nearest, already, C) -> None:
    path = Path("logs/signals_latest.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Signals side feature",
        "",
        f"Written {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"Last closed daily bar: {as_of}",
        f"BTC closed {btc:.2f}  SMA200 {sma200:.2f}  gate {'ON' if gate_on else 'OFF'}",
        "",
        "Not a live order. Strategy has no take-profit. Live price above a level is not a buy until the daily bar closes.",
        "",
        "## In book",
        "",
    ]
    if held.empty:
        lines.append("None.")
    else:
        for coin, wt in held.items():
            st = state.get(coin, {})
            lines.append(f"- {coin} weight {wt:.3f} entry {st.get('entry')} SL {st.get('stop')}")
    lines.extend(["", "## Closest, still under the level", ""])
    for g, coin, px, brk, _closed in nearest:
        lines.append(f"- {coin} live {px:.6g} need {brk:.6g} gap {g:.1%}")
    lines.extend(["", "## Live above level, unconfirmed", ""])
    if not already:
        lines.append("None.")
    else:
        for g, coin, px, brk, _closed in already:
            lines.append(f"- {coin} live {px:.6g} level {brk:.6g} above {-g:.1%}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
