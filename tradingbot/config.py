"""YAML + env config. Default mode is dry_run. Live mode is always refused."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional


class LiveTradingRefused(RuntimeError):
    """Raised when live mode is requested without the required gates, or for any live attempt."""


class AllocatedCapitalRequired(ValueError):
    """allocated_capital is required and is never derived from the main OKX account balance."""


# ---- minimal YAML subset loader (no PyYAML dependency) ----

def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "" or s in ("null", "None", "~"):
        return None
    if s in ("true", "True", "yes", "YES"):
        return True
    if s in ("false", "False", "no", "NO"):
        return False
    if (len(s) >= 2) and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    try:
        if s.startswith(("+", "-")):
            body = s[1:]
        else:
            body = s
        if body.isdigit():
            return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _strip_comment(line: str) -> str:
    out = []
    in_s = None
    for ch in line:
        if in_s:
            out.append(ch)
            if ch == in_s:
                in_s = None
            continue
        if ch in ('"', "'"):
            in_s = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def load_yaml(text: str) -> dict:
    """Indent-based mapping/list YAML subset used by config.yaml."""
    raw_lines = text.splitlines()
    items: list[tuple[int, str]] = []
    for line in raw_lines:
        stripped = _strip_comment(line)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        items.append((indent, stripped.strip()))

    def parse_block(idx: int, indent: int):
        # mapping or list at this indent
        mapping: dict[str, Any] = {}
        lst: list[Any] = []
        is_list = None
        while idx < len(items):
            ind, content = items[idx]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError(f"Unexpected indent at: {content}")
            if content.startswith("- "):
                if is_list is False:
                    raise ValueError("mixed list/map")
                is_list = True
                rest = content[2:].strip()
                if ":" in rest and not rest.startswith("{"):
                    key, _, val = rest.partition(":")
                    key = key.strip()
                    val = val.strip()
                    child = {}
                    if val:
                        child[key] = _parse_scalar(val)
                    if idx + 1 < len(items) and items[idx + 1][0] > indent:
                        sub, idx = parse_block(idx + 1, items[idx + 1][0])
                        if isinstance(sub, dict):
                            child.update(sub)
                        idx -= 1
                    else:
                        idx += 1
                        lst.append(child if child else _parse_scalar(rest))
                        continue
                    lst.append(child)
                    continue
                if idx + 1 < len(items) and items[idx + 1][0] > indent:
                    sub, idx = parse_block(idx + 1, items[idx + 1][0])
                    lst.append(sub)
                    continue
                lst.append(_parse_scalar(rest) if rest else None)
                idx += 1
                continue
            is_list = False
            if ":" not in content:
                raise ValueError(f"Expected key: value, got {content}")
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                mapping[key] = _parse_scalar(val)
                idx += 1
            else:
                if idx + 1 < len(items) and items[idx + 1][0] > indent:
                    sub, idx = parse_block(idx + 1, items[idx + 1][0])
                    mapping[key] = sub
                else:
                    mapping[key] = None
                    idx += 1
        return (lst if is_list else mapping), idx

    doc, _ = parse_block(0, items[0][0] if items else 0)
    if not isinstance(doc, dict):
        raise ValueError("config root must be a mapping")
    return doc


def _deep_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


@dataclass
class RiskConfig:
    risk_pct: float = 0.0075
    risk_pct_min: float = 0.005
    risk_pct_max: float = 0.01
    daily_loss_cap: float = 0.03
    max_drawdown: float = 0.10
    max_exposure_asset: float = 0.25
    max_exposure_total: float = 1.00
    max_open_positions: int = 20
    min_notional: float = 10.0
    lot_size: float = 0.00001
    tick_size: float = 0.1

    def clamped_risk_pct(self) -> float:
        return min(self.risk_pct_max, max(self.risk_pct_min, self.risk_pct))


@dataclass
class DataConfig:
    rest_base: str = "https://www.okx.com/api/v5"
    ws_public: str = "wss://ws.okx.com:8443/ws/v5/public"
    cache_dir: str = "data/cache"
    timeout_sec: float = 10.0


@dataclass
class BacktestConfig:
    days: int = 365
    n_folds: int = 4
    oos_ratio: float = 0.20
    symbol: str = "BTC-USDT"
    bar: str = "1H"


@dataclass
class Config:
    mode: str = "dry_run"  # dry_run | paper | live (live always refused)
    exchange: str = "okx"
    market: str = "spot"
    symbol: str = "BTC-USDT"
    bar: str = "1H"
    fee_taker: float = 0.001
    slippage_mult: float = 2.0  # slippage = fee_taker * slippage_mult (default 2x fee)
    # Required fixed sub-account allocation. NEVER derived from OKX account balance.
    allocated_capital: float | None = None
    # Spec v2 official paper notional. Required for v2 paper; never from OKX or 6788.
    c_paper: float | None = None
    state_path: str = "state/portfolio.json"
    log_path: str = "logs/tradingbot.jsonl"
    timezone: str = "Etc/GMT-7"
    universe_file: str = "/workspace/crypto/favorites.txt"
    crypto_data_dir: str = "/workspace/crypto/data"  # read-only seed; never write here
    paper_log_path: str = "logs/paper_book.jsonl"
    risk: RiskConfig = field(default_factory=RiskConfig)
    data: DataConfig = field(default_factory=DataConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    # telegram token is NEVER stored here; read from env at alert time only
    source_path: Optional[str] = None

    def __post_init__(self) -> None:
        if self.allocated_capital is None:
            raise AllocatedCapitalRequired(
                "allocated_capital is required (fixed dedicated-sub-account allocation). "
                "It is never read from the main OKX account balance."
            )
        self.allocated_capital = float(self.allocated_capital)
        if self.allocated_capital <= 0:
            raise AllocatedCapitalRequired("allocated_capital must be a positive number.")
        if self.c_paper is not None:
            self.c_paper = float(self.c_paper)
            if self.c_paper <= 0:
                raise AllocatedCapitalRequired("C_paper must be a positive number.")
            if abs(self.c_paper - 6788.0) < 1e-9:
                raise AllocatedCapitalRequired(
                    "C_paper equals the excluded 6,788 USDT wedding-funds figure; refuse."
                )

    @property
    def slippage(self) -> float:
        return self.fee_taker * self.slippage_mult

    @property
    def paper_notional_usdt(self) -> float:
        """Alias required by the data contract (§8). Always the configured allocation."""
        return float(self.allocated_capital)


def _risk_from(d: dict) -> RiskConfig:
    r = RiskConfig()
    src = d.get("risk") or {}
    for k in r.__dataclass_fields__:
        if k in src and src[k] is not None:
            setattr(r, k, type(getattr(r, k))(src[k]))
    return r


def _data_from(d: dict) -> DataConfig:
    c = DataConfig()
    src = d.get("data") or {}
    for k in c.__dataclass_fields__:
        if k in src and src[k] is not None:
            setattr(c, k, type(getattr(c, k))(src[k]))
    return c


def _bt_from(d: dict) -> BacktestConfig:
    c = BacktestConfig()
    src = d.get("backtest") or {}
    for k in c.__dataclass_fields__:
        if k in src and src[k] is not None:
            setattr(c, k, type(getattr(c, k))(src[k]))
    return c



def _resolve_capital(data: dict) -> float | None:
    """allocated_capital and paper_notional_usdt are the same required paper allocation.

    Neither may be inferred from an OKX account balance, OKX_* env, or the excluded
    main-account Simple Earn figure. If both keys are present they must match.
    """
    ac = data.get("allocated_capital")
    pn = data.get("paper_notional_usdt")
    if ac is None and pn is None:
        return None  # __post_init__ raises AllocatedCapitalRequired
    if ac is not None and pn is not None and float(ac) != float(pn):
        raise AllocatedCapitalRequired(
            "allocated_capital and paper_notional_usdt must be equal when both are set; "
            "neither is read from the OKX account."
        )
    return float(ac if ac is not None else pn)



def _resolve_c_paper(data: dict) -> float | None:
    """C_paper is the spec v2 paper notional. Never inferred from an OKX balance or 6788."""
    raw = data.get("C_paper", data.get("c_paper"))
    if raw is None:
        return None
    return float(raw)


def load_config(path: Optional[str] = None) -> Config:
    path = path or os.environ.get("TRADINGBOT_CONFIG") or "config.yaml"
    p = Path(path)
    data: dict = {}
    if p.exists():
        data = load_yaml(p.read_text()) or {}
    cfg = Config(
        mode=str(data.get("mode") or "dry_run"),
        exchange=str(data.get("exchange") or "okx"),
        market=str(data.get("market") or "spot"),
        symbol=str(data.get("symbol") or "BTC-USDT"),
        bar=str(data.get("bar") or "1H"),
        fee_taker=float(data.get("fee_taker") if data.get("fee_taker") is not None else 0.001),
        slippage_mult=float(data.get("slippage_mult") if data.get("slippage_mult") is not None else 2.0),
        allocated_capital=_resolve_capital(data),
        c_paper=_resolve_c_paper(data),
        state_path=str(data.get("state_path") or "state/portfolio.json"),
        log_path=str(data.get("log_path") or "logs/tradingbot.jsonl"),
        timezone=str(data.get("timezone") or "Etc/GMT-7"),
        universe_file=str(data.get("universe_file") or "/workspace/crypto/favorites.txt"),
        crypto_data_dir=str(data.get("crypto_data_dir") or "/workspace/crypto/data"),
        paper_log_path=str(data.get("paper_log_path") or "logs/paper_book.jsonl"),
        risk=_risk_from(data),
        data=_data_from(data),
        backtest=_bt_from(data),
        source_path=str(p) if p.exists() else None,
    )
    env_mode = os.environ.get("TRADINGBOT_MODE")
    if env_mode:
        cfg.mode = env_mode.strip()
    return cfg


def ensure_safe_mode(cfg: Config, live_flag: bool = False) -> None:
    """Refuse live trading always.

    Live would require ALL of: explicit --live flag, env LIVE_APPROVED=1, and mode=live.
    Even when all three are present, live is not implemented and we still refuse.
    """
    wants_live = (cfg.mode == "live") or live_flag
    if not wants_live:
        if cfg.mode not in ("dry_run", "paper"):
            raise LiveTradingRefused(f"Unknown mode {cfg.mode!r}. Allowed: dry_run, paper.")
        return
    approved = os.environ.get("LIVE_APPROVED") == "1"
    if not (live_flag and approved and cfg.mode == "live"):
        raise LiveTradingRefused(
            "Live mode requires explicit --live flag AND env LIVE_APPROVED=1 "
            "AND mode=live. Refusing."
        )
    raise NotImplementedError(
        "Live trading is not implemented. Even with --live and LIVE_APPROVED=1, refusing."
    )
