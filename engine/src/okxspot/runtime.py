from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from okxspot.alerts import Alerter, build_alerter
from okxspot.backtest.report import write_backtest_report
from okxspot.backtest.session import SessionResult, build_strategy, run_session
from okxspot.config import Settings
from okxspot.demo_data import synthetic_market
from okxspot.engine import TradingEngine, build_engine, restore_paper
from okxspot.exchange.okx_client import OkxClient
from okxspot.exchange.okx_exchange import OkxExchange
from okxspot.exchange.paper import PaperExchange
from okxspot.exchange.ws import default_connect
from okxspot.instruments import default_instruments
from okxspot.logging_config import Log
from okxspot.marketdata import PublicFeed
from okxspot.exchange.okx_parse import AccountConfig
from okxspot.safety import (
    OkxKeys,
    confirm_live_env,
    read_okx_keys,
    validate_account_restrictions,
    validate_live_preconditions,
    validate_permissions,
)
from okxspot.state import StateStore

log = Log("okxspot.runtime")


def connect_okx(settings: Settings, keys: OkxKeys) -> OkxClient:
    return OkxClient(
        base_url=settings.okx.rest_base,
        api_key=keys.api_key,
        secret=keys.secret,
        passphrase=keys.passphrase,
        timeout_s=settings.okx.timeout_s,
        max_retries=settings.okx.max_retries,
        rate_limit_per_sec=settings.okx.rate_limit_per_sec,
    )


def fetch_account_config(client: OkxClient, settings: Settings) -> AccountConfig:
    exchange = OkxExchange(
        client,
        quote_ccy=settings.quote_ccy,
        min_notional=settings.okx.min_notional_usdt,
        bar=settings.bar,
    )
    client.sync_clock()
    return exchange.get_account_config()


def fetch_permissions(client: OkxClient, settings: Settings) -> set[str]:
    return fetch_account_config(client, settings).permissions


def prepare_live(settings: Settings, *, enable_live_flag: bool, env: Mapping[str, str]) -> OkxClient:
    """Run every live gate before any order can be built."""
    validate_live_preconditions(
        enable_live_flag=enable_live_flag,
        live_enabled=settings.live_enabled,
        env_confirm=confirm_live_env(env),
        allocated_capital=settings.allocated_capital,
        max_allocated_capital=settings.max_allocated_capital,
    )
    keys = read_okx_keys(env)
    client = connect_okx(settings, keys)
    account = fetch_account_config(client, settings)
    validate_permissions(account.permissions)
    validate_account_restrictions(account, settings)
    log.info(
        "live gates passed",
        allocated_capital=str(settings.allocated_capital),
        permissions=",".join(sorted(account.permissions)),
    )
    return client


def run_demo(settings: Settings, *, output: Path | None = None) -> SessionResult:
    candles = synthetic_market()
    result = run_session(
        candles,
        settings,
        fee_rate=settings.fees.rate_for("paper"),
    )
    if output is not None:
        write_backtest_report(result.report, output)
    return result


def run_backtest(settings: Settings, *, output: Path) -> SessionResult:
    result = run_session(
        synthetic_market(),
        settings,
        fee_rate=settings.fees.rate_for("backtest"),
    )
    write_backtest_report(result.report, output)
    return result


def run_backtest_history(
    settings: Settings,
    *,
    start: datetime,
    end: datetime,
    output: Path,
) -> SessionResult:
    from okxspot.backtest.download import download_candles

    exchange = _public_okx(settings)
    cache = Path(settings.backtest.cache_dir)
    candles = {
        symbol: download_candles(
            exchange,
            symbol,
            settings.bar,
            start,
            end,
            cache_dir=cache,
        )
        for symbol in _feed_symbols(settings)
    }
    result = run_session(candles, settings, fee_rate=settings.fees.rate_for("backtest"))
    write_backtest_report(result.report, output)
    return result


def run_paper_loop(settings: Settings, *, once: bool, demo: bool) -> SessionResult | None:
    if demo:
        return run_demo(settings)
    feed_exchange = _public_okx(settings)
    feed = PublicFeed(
        feed_exchange,
        ws_url=settings.okx.ws_public,
        symbols=_feed_symbols(settings),
        bar=settings.bar,
        connect=default_connect,
    )
    state = StateStore(settings.state.sqlite_path)
    paper = _paper_from_settings(settings, state)
    engine = build_engine(
        exchange=paper,
        settings=settings,
        strategy=build_strategy(settings),
        state=state,
        fee_rate=settings.fees.rate_for("paper"),
        alerter=build_alerter(settings),
    )
    restore_paper(state, paper)
    _loop(engine, feed, settings, once=once, live=False)
    return None


def run_live_loop(settings: Settings, client: OkxClient, *, once: bool) -> None:
    exchange = OkxExchange(
        client,
        quote_ccy=settings.quote_ccy,
        min_notional=settings.okx.min_notional_usdt,
        bar=settings.bar,
    )
    for symbol in _feed_symbols(settings):
        exchange.get_instrument(symbol)
    state = StateStore(settings.state.sqlite_path)
    alerter: Alerter = build_alerter(settings)
    engine = build_engine(
        exchange=exchange,
        settings=settings,
        strategy=build_strategy(settings),
        state=state,
        fee_rate=settings.fees.rate_for("live"),
        alerter=alerter,
    )
    report = engine.orders.reconcile()
    engine.sync_exchange_fills()
    log.info(
        "reconciled",
        adopted=len(report.adopted),
        updated=len(report.updated),
        missing=len(report.missing),
        filled=len(report.filled),
    )
    feed = PublicFeed(
        exchange,
        ws_url=settings.okx.ws_public,
        symbols=_feed_symbols(settings),
        bar=settings.bar,
        connect=default_connect,
    )
    _loop(engine, feed, settings, once=once, live=True)


def _loop(
    engine: TradingEngine,
    feed: PublicFeed,
    settings: Settings,
    *,
    once: bool,
    live: bool,
) -> None:
    while True:
        candles = feed.next_candles()
        if feed.transport == "rest" and feed.fallback_reason:
            engine.alerter.send(
                "warning",
                f"market data is on REST polling after websocket failure: {feed.fallback_reason}",
            )
            feed.fallback_reason = None
        engine.ingest_latest(candles)
        if live:
            engine.sync_exchange_fills()
            engine.enforce_software_stops()
        engine.orders.reconcile()
        if once:
            return
        time.sleep(settings.runtime.poll_seconds)


def _public_okx(settings: Settings) -> OkxExchange:
    client = OkxClient(
        base_url=settings.okx.rest_base,
        timeout_s=settings.okx.timeout_s,
        max_retries=settings.okx.max_retries,
        rate_limit_per_sec=settings.okx.rate_limit_per_sec,
    )
    return OkxExchange(
        client,
        quote_ccy=settings.quote_ccy,
        min_notional=settings.okx.min_notional_usdt,
        bar=settings.bar,
    )


def _paper_from_settings(settings: Settings, state: StateStore) -> PaperExchange:
    symbols = _feed_symbols(settings)
    return PaperExchange(
        quote_ccy=settings.quote_ccy,
        cash=settings.allocated_capital,
        taker_fee=settings.fees.rate_for("paper"),
        slippage_bps=settings.fees.slippage_bps,
        partial_fill_ratio=settings.fees.partial_fill_ratio,
        instruments=default_instruments(symbols, settings.okx.min_notional_usdt),
        immediate=False,
    )


def _feed_symbols(settings: Settings) -> list[str]:
    symbols = list(settings.symbols)
    if settings.regime_symbol not in symbols:
        symbols.append(settings.regime_symbol)
    return symbols
