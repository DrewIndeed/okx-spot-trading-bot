from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from okxspot.alerts import Alerter, build_alerter
from okxspot.atomic import write_text_atomic
from okxspot.backtest.report import write_backtest_report
from okxspot.backtest.session import SessionResult, build_strategy, run_session
from okxspot.config import Settings
from okxspot.config import LIVE_MAX_DRAWDOWN_PCT
from okxspot.errors import LiveGuardError
from okxspot.killswitch import KillSwitch
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
    SUPERVISED_ENV,
    OkxKeys,
    confirm_live_env,
    read_okx_keys,
    validate_account_restrictions,
    validate_live_preconditions,
    validate_permissions,
)
from okxspot.stale import StaleDataError
from okxspot.state import StateStore
from okxspot.strategy import donchian

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



def assert_start_allowed(settings: Settings) -> None:
    """Refuse paper/live start while the file kill switch is tripped or missing."""
    ks = KillSwitch(settings.kill_switch_path)
    if ks.is_tripped:
        raise LiveGuardError(
            f"kill switch is tripped ({ks.reason or 'missing'}); requires a manual re-arm"
        )


def require_supervisor(env: Mapping[str, str]) -> None:
    if str(env.get(SUPERVISED_ENV) or "").strip() not in {"1", "true", "yes", "on"}:
        raise LiveGuardError(
            f"long-running live mode requires a supervisor ({SUPERVISED_ENV}=1)"
        )


def assert_live_strategy(settings: Settings) -> None:
    if settings.allow_placeholder_live:
        return
    if getattr(donchian, "IS_PLACEHOLDER", False) and settings.strategy.name == "donchian_atr":
        raise LiveGuardError("refusing live start: donchian_atr is a placeholder strategy")


def maybe_recheck_account(engine: TradingEngine, settings: Settings, *, account: AccountConfig, total_eq: Decimal | None = None) -> None:
    """Mid-run account-config changes must trip the kill switch, not be ignored."""
    try:
        validate_account_restrictions(account, settings)
        cap = settings.max_allocated_capital
        if total_eq is not None and cap > 0 and total_eq > cap * Decimal("10"):
            raise LiveGuardError("account totalEq far exceeds allocated capital")
    except LiveGuardError as exc:
        if engine.kill_switch is not None:
            engine.kill_switch.trip(str(exc), source="account_recheck")
        engine.risk.halted = True
        engine.risk.halt_reason = "account_recheck"
        engine.alerter.send("critical", f"account recheck failed: {exc}", event="kill_switch")


def recheck_account_from_exchange(engine: TradingEngine, settings: Settings) -> None:
    """Call maybe_recheck_account from the live/paper loop when the venue can answer."""
    getter = getattr(engine.exchange, "get_account_config", None)
    if not callable(getter):
        return
    try:
        account = getter()
    except Exception as exc:
        maybe_recheck_account(
            engine,
            settings,
            account=__import__("okxspot.exchange.okx_parse", fromlist=["AccountConfig"]).AccountConfig(
                permissions=set(), ip=None, uid="", main_uid="", label=""
            ),
            total_eq=None,
        )
        if engine.kill_switch is not None and not engine.kill_switch.is_tripped:
            engine.kill_switch.trip(f"account_recheck_failed:{type(exc).__name__}", source="account_recheck")
        engine.risk.halted = True
        engine.risk.halt_reason = "account_recheck"
        return
    total_eq = None
    try:
        view = engine.exchange.get_account_view()
        total_eq = getattr(view, "total_eq", None)
    except Exception:
        total_eq = None
    maybe_recheck_account(engine, settings, account=account, total_eq=total_eq)


def _write_child_heartbeat(settings: Settings, now: datetime) -> None:
    path = Path(settings.child_heartbeat_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": now.astimezone(UTC).isoformat() if now.tzinfo else now.replace(tzinfo=UTC).isoformat(),
        "pid": os.getpid(),
        "running": True,
    }
    write_text_atomic(path, json.dumps(payload) + "\n")


def prepare_live(settings: Settings, *, enable_live_flag: bool, env: Mapping[str, str]) -> OkxClient:
    """Run every live gate before any order can be built."""
    if settings.mode != "live":
        raise LiveGuardError("live mode requires settings.mode == 'live'")
    if settings.risk.max_drawdown_pct > LIVE_MAX_DRAWDOWN_PCT:
        raise LiveGuardError(
            f"live mode requires risk.max_drawdown_pct <= {LIVE_MAX_DRAWDOWN_PCT}; "
            f"got {settings.risk.max_drawdown_pct}"
        )
    require_supervisor(env)
    assert_live_strategy(settings)
    if not settings.required_uid or not settings.required_account_label:
        raise LiveGuardError("live mode requires required_uid and required_account_label")
    validate_live_preconditions(
        enable_live_flag=enable_live_flag,
        live_enabled=settings.live_enabled,
        env_confirm=confirm_live_env(env),
        allocated_capital=settings.allocated_capital,
        max_allocated_capital=settings.max_allocated_capital,
    )
    assert_start_allowed(settings)
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
    assert_start_allowed(settings)
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
    assert_start_allowed(settings)
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
    recheck_account_from_exchange(engine, settings)
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
    now: Callable[[], datetime] | None = None,
) -> None:
    clock = now or (lambda: datetime.now(UTC))
    while True:
        moment = clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        try:
            _write_child_heartbeat(settings, moment)
        except OSError:
            pass
        engine.apply_kill_switch()
        recheck_account_from_exchange(engine, settings)
        try:
            candles = feed.next_candles(timeout=settings.runtime.poll_seconds)
        except TypeError:
            candles = feed.next_candles()
        if getattr(feed, "transport", None) == "rest" and getattr(feed, "fallback_reason", None):
            engine.alerter.send(
                "warning",
                f"market data is on REST polling after websocket failure: {feed.fallback_reason}",
            )
            feed.fallback_reason = None
        # Sync, software stops, and flatten retries must run even when the
        # feed only has unconfirmed mid-session bars.
        engine.sync_exchange_fills()
        engine.enforce_software_stops()
        tripped = engine.kill_switch is not None and engine.kill_switch.is_tripped
        if (engine.risk.halted or tripped) and engine.book.positions:
            reason = engine.risk.halt_reason or (engine.kill_switch.reason if engine.kill_switch else "halt")
            engine.force_flatten(reason)
        tradeable = True
        if engine.stale is not None:
            try:
                last_price = None
                # Prefer a live ticker when present; a failure is not treated as stale here
                # (software stops already fail closed on ticker errors).
                try:
                    symbol = settings.symbols[0]
                    last_price = engine.exchange.get_ticker(symbol).last
                except Exception:
                    last_price = None
                series: list = []
                for bars in candles.values():
                    series.extend(bars)
                engine.stale.assert_tradeable(
                    candles=series,
                    now=moment,
                    server_time_ms=engine.exchange.server_time_ms(),
                    last_price=last_price,
                )
            except StaleDataError as exc:
                tradeable = False
                engine.alerter.send("warning", f"stale data: {exc}", event="stale_data")
        if tradeable and not tripped and not engine.risk.halted:
            engine.ingest_latest(candles)
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
