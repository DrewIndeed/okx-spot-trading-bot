"""Fast live summary. One OKX ticker call plus cached daily closes.

Watchlist only. It does not run the slow breakout book, and it does not place orders.
A live price above the prior 20-day high is not a buy until that daily bar closes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tradingbot.config import Config
from tradingbot.data.rest import OkxRestFeed
from tradingbot.termfmt import kv, pct, px, table, title
from tradingbot.universe import base_sym, inst_id, load_favorites


def _bars(cache_dir: Path, base: str) -> pd.DataFrame | None:
    path = cache_dir / f"{inst_id(base)}_1Dutc.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except ImportError as err:
        raise SystemExit(
            "Parquet cache needs pyarrow. Run: pip install pyarrow"
        ) from err
    if "confirm" in df.columns:
        df = df[df["confirm"] == 1]
    if df.empty or "close" not in df.columns:
        return None
    out = pd.DataFrame(
        {
            "close": df["close"].astype(float).to_numpy(),
            "quote": df["volume_quote"].astype(float).to_numpy() if "volume_quote" in df.columns else 0.0,
        },
        index=pd.to_datetime(df["ts"], unit="ms", utc=True),
    )
    out = out.sort_index()
    out.index.name = None
    return out[~out.index.duplicated(keep="last")]


def print_summary(cfg: Config, *, top: int = 8, symbol: str | None = None) -> int:
    feed = OkxRestFeed(
        cache_dir=cfg.data.cache_dir,
        rest_base=cfg.data.rest_base,
        timeout_sec=cfg.data.timeout_sec,
        crypto_data_dir=cfg.crypto_data_dir,
    )
    tickers = feed.get_spot_tickers()
    bases = load_favorites(cfg.universe_file)
    frames: dict[str, pd.DataFrame] = {}
    missing = 0
    for base in bases:
        frame = _bars(Path(cfg.data.cache_dir), base)
        if frame is None or len(frame) < 21:
            missing += 1
            continue
        frames[base_sym(base)] = frame
    if "BTC" not in frames:
        print("No BTC daily cache. Run: python -m tradingbot data-sync --bars 1Dutc")
        return 1

    close_panel = pd.DataFrame({k: v["close"] for k, v in frames.items()}).sort_index()
    quote_panel = pd.DataFrame({k: v["quote"] for k, v in frames.items()}).reindex(close_panel.index)
    btc = close_panel["BTC"].dropna()
    sma200 = float(btc.rolling(200).mean().iloc[-1]) if len(btc) >= 200 else float("nan")
    btc_close = float(btc.iloc[-1])
    btc_live = tickers.get("BTC-USDT")
    gate_on = btc_close > sma200 if sma200 == sma200 else False
    age_h = (datetime.now(timezone.utc) - btc.index[-1].to_pydatetime()).total_seconds() / 3600
    hist = close_panel.notna().sum()
    med_vol = quote_panel.rolling(30, min_periods=20).median().iloc[-1]
    avg_vol = quote_panel.rolling(30, min_periods=20).mean().iloc[-1]
    eligible_mask = (hist >= 200) & (med_vol >= 2_000_000.0)
    top20 = set(avg_vol[eligible_mask].nlargest(20).index)
    sma50 = close_panel.rolling(50, min_periods=50).mean().iloc[-1]
    last_close = close_panel.iloc[-1]
    if top20:
        breadth = sum(float(last_close[c]) > float(sma50[c]) for c in top20 if c in last_close.index and sma50[c] == sma50[c]) / len(top20)
    else:
        breadth = 0.0
    breadth_ok = breadth >= 0.5

    print("OKX spot paper summary")
    print(f"live tickers + cached daily bars    closed {btc.index[-1].date()}    age {age_h:.1f}h    names {len(frames)}    missing {missing}")
    if age_h > 36:
        print("Cache is stale. Run: python -m tradingbot data-sync --bars 1Dutc")
    print(kv([
        ("BTC last close", px(btc_close)),
        ("BTC live", px(btc_live.last) if btc_live else "n/a"),
        ("BTC SMA200", px(sma200)),
        ("Gate", "ON" if gate_on else "OFF"),
        ("Breadth", f"{breadth:.0%} of top-20 above SMA50"),
        ("New entries", "yes" if gate_on and breadth_ok else "no"),
        ("Buys", "none until a daily close confirms"),
    ]))

    prev_high = close_panel.shift(1).rolling(20, min_periods=20).max().iloc[-1]
    channel_mid = ((close_panel.rolling(20, min_periods=20).max() + close_panel.rolling(20, min_periods=20).min()) / 2).iloc[-1]
    rows = []
    for coin in close_panel.columns:
        tick = tickers.get(inst_id(coin))
        if tick is None or prev_high.get(coin) != prev_high.get(coin):
            continue
        level = float(prev_high[coin])
        mid = float(channel_mid[coin]) if channel_mid.get(coin) == channel_mid.get(coin) else float("nan")
        if level <= 0:
            continue
        gap = level / tick.last - 1.0
        rows.append((0 if coin in top20 else 1, gap, coin, tick.last, level, mid, coin in top20))
    rows.sort()

    eligible_rows = [r for r in rows if r[6]]
    under = [r for r in eligible_rows if r[1] > 0][:top]
    above = [r for r in eligible_rows if r[1] <= 0][:top]
    under_rows = []
    for _rank, gap, coin, live, level, mid, _ok in under:
        sl = "yes" if mid < live else "no"
        under_rows.append([coin, px(live), px(level), pct(gap), sl])
    print(title(
        "Closest eligible, still under the 20-day high",
        "top-20 by 30d quote volume, history >= 200d, median vol >= $2M",
    ))
    print(table(
        ["Coin", "Live", "Needs above", "Gap", "Stop ok"],
        under_rows,
        right={1, 2, 3},
    ))
    above_rows = [[coin, px(live), px(level), pct(-gap)] for _rank, gap, coin, live, level, _mid, _ok in above]
    print(title("Live above the old high", "Still not a buy until the daily bar closes."))
    print(table(["Coin", "Live", "Level", "Above"], above_rows, right={1, 2, 3}))

    if symbol:
        raw = symbol.upper().replace("-USDT", "")
        hit = next((r for r in rows if r[2] == raw), None)
        print(title(raw))
        if hit is None:
            print("Not in cache, or no live ticker.")
            return 1
        _rank, gap, _coin, live, level, mid, in_top = hit
        pairs = [
            ("Live", px(live)),
            ("Breakout level", px(level)),
            ("Gap", pct(gap)),
            ("In liquidity top-20", "yes" if in_top else "no"),
            ("Strategy buy", "no, until a daily close confirms"),
        ]
        if mid >= live:
            pairs.append(("Stop", f"none. 20-day mid {px(mid)} is above the live price"))
        else:
            pairs.append(("Hypothetical SL", px(mid)))
            pairs.append(("Hypothetical TP 2R", px(live + 2 * (live - mid))))
            pairs.append(("Note", "label only, not a strategy exit"))
        print(kv(pairs))
    print(title("Note", "Paper only. No live order. Exit is a close under the stop, or BTC back under SMA200."))
    return 0
