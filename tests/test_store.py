from tradingbot.data.store import cache_backend, load_candles, save_candles
from tradingbot.types import Candle


def _c(ts, o=1.0, confirm=1):
    return Candle(ts=ts, open=o, high=o + 1, low=o - 0.5, close=o + 0.2, volume_base=10, volume_quote=20, confirm=confirm)


def test_cache_backend_is_parquet_or_csv():
    assert cache_backend() in ("parquet", "csv")


def test_roundtrip_default_backend(tmp_path):
    rows = [_c(1_000), _c(2_000, confirm=0)]
    path = save_candles(tmp_path, "BTC-USDT", "4H", rows)
    assert path.suffix in (".parquet", ".csv")
    loaded = load_candles(tmp_path, "BTC-USDT", "4H")
    assert len(loaded) == 2
    assert loaded[0].ts == 1_000
    assert loaded[0].open == 1.0
    assert loaded[0].high == 2.0
    assert loaded[1].confirm == 0


def test_load_legacy_json(tmp_path):
    import json
    p = tmp_path / "ETH-USDT_1Dutc.json"
    p.write_text(json.dumps([[10, 2, 3, 1, 2.5, 4, 5, 1]]))
    loaded = load_candles(tmp_path, "ETH-USDT", "1Dutc")
    assert len(loaded) == 1 and loaded[0].close == 2.5
