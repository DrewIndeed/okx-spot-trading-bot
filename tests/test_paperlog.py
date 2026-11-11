from tradingbot.paperlog import (
    make_day,
    make_init,
    write_day_idempotent,
    write_init_if_needed,
)


def test_init_and_day_schema_and_idempotent(tmp_path):
    path = tmp_path / "paper.jsonl"
    init = make_init(
        date="2026-11-10",
        allocated_capital=10_000,
        sleeve_weights={"D1": 0.0},
        positions={"D1": {}},
        s1_stops={},
        state_as_of_close="2026-11-10",
    )
    for k in ("type", "date", "created", "book_equity", "d1_equity", "state_as_of_close",
              "sleeve_weights", "positions", "s1_stops", "spec"):
        assert k in init
    assert init["type"] == "init"
    assert init["allocated_capital"] == 10_000
    assert write_init_if_needed(path, init) is True
    assert write_init_if_needed(path, init) is False  # already present

    day = make_day(
        date="2026-11-11",
        allocated_capital=10_000,
        sleeve_weights={"D1": 0.1},
        positions={"D1": {"BTC": 0.1}},
        book_ret=0.01,
        d1_ret=0.01,
        book_equity=10_100,
        d1_equity=10_100,
        book_trades_closed=[],
        d1_trades_closed=[],
    )
    for k in ("type", "date", "sleeve_weights", "positions", "book_ret", "d1_ret",
              "book_equity", "d1_equity", "book_trades_closed", "d1_trades_closed"):
        assert k in day
    assert write_day_idempotent(path, day, ready_date="2026-11-11") is True
    assert write_day_idempotent(path, day, ready_date="2026-11-11") is False
    # future day not ready
    day2 = dict(day, date="2026-11-12")
    assert write_day_idempotent(path, day2, ready_date="2026-11-11") is False
