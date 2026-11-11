from tradingbot.data.pipeline import SyncResult, write_quality_report
from tradingbot.data.quality import assess
from tradingbot.types import Candle


def _c(ts, o=10.0, h=11.0, l=9.0, c=10.5, vb=1.0, vq=10.0, confirm=1):
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume_base=vb, volume_quote=vq, confirm=confirm)


IV = 14_400_000  # 4H


def test_clean_series_ok():
    rows = [_c(i * IV) for i in range(10)]
    q = assess(rows, "4H", "BTC-USDT")
    assert q["ok"] is True
    assert q["n_closed"] == 10
    assert q["gaps"] == 0
    assert q["duplicates"] == 0


def test_detects_gap_duplicate_nonmono_nan_zero():
    t0 = 0
    rows = [
        _c(t0),
        _c(t0 + IV),
        _c(t0 + 3 * IV),  # gap of one 4H bar
        _c(t0 + 3 * IV),  # duplicate + non-monotonic equal
        _c(t0 + 4 * IV, o=float("nan"), c=float("nan")),
        _c(t0 + 5 * IV, o=0.0, h=0.0, l=0.0, c=0.0),
        _c(t0 + 6 * IV, vb=0.0, vq=0.0),
    ]
    q = assess(rows, "4H", "AAA-USDT")
    assert q["ok"] is False
    assert q["gaps"] == 1
    assert q["duplicates"] == 1
    assert q["non_monotonic"] >= 1
    assert q["nan_ohlcv"] == 1
    assert q["zero_ohlc"] == 1
    assert q["zero_volume"] == 1
    assert any(x.startswith("gaps:") for x in q["issues"])
    assert any(x.startswith("zero_volume:") for x in q["warnings"])


def test_quality_report_files(tmp_path):
    rows = [_c(i * IV) for i in range(5)]
    q = assess(rows, "4H", "BTC-USDT")
    result = SyncResult(backend="parquet", bars=["4H"], symbols=["BTC-USDT"], series=[q])
    result.started_ts = 0
    result.finished_ts = 1
    jp, mp = write_quality_report(result, tmp_path)
    assert (tmp_path / "quality_report.json").exists()
    assert (tmp_path / "quality_report.md").exists()
    text = (tmp_path / "quality_report.md").read_text()
    assert "BTC-USDT" in text and "Closed" in text
