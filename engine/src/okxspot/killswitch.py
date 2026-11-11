from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from okxspot.errors import OkxSpotError


class KillSwitchError(OkxSpotError):
    """Kill-switch refused an automatic re-arm or an invalid state change."""


class KillSwitch:
    """File-backed kill switch. Tripped state survives restarts; only a manual re-arm clears it."""

    def __init__(
        self,
        path: str | Path,
        *,
        on_cancel_open: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._on_cancel_open = on_cancel_open
        self._state: dict[str, object] = {"tripped": False, "reason": "", "cancel_open": False}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            self._state.update(raw)

    def _dump(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @property
    def is_tripped(self) -> bool:
        return bool(self._state.get("tripped"))

    @property
    def reason(self) -> str:
        return str(self._state.get("reason") or "")

    @property
    def cancel_open(self) -> bool:
        return bool(self._state.get("cancel_open"))

    def allows_new_orders(self) -> bool:
        return not self.is_tripped

    def trip(
        self,
        reason: str,
        *,
        cancel_open: bool = False,
        at: datetime | None = None,
        source: str = "manual",
    ) -> None:
        already = self.is_tripped
        moment = at or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        self._state = {
            "tripped": True,
            "reason": reason,
            "cancel_open": cancel_open,
            "source": source,
            "ts": moment.astimezone(UTC).isoformat(),
            "rearmed": False,
        }
        self._dump()
        if cancel_open and not already and self._on_cancel_open is not None:
            self._on_cancel_open()

    def rearm(self, *, operator: str = "manual") -> None:
        if not self.is_tripped and not self.path.exists():
            self._state = {"tripped": False, "reason": "", "cancel_open": False, "rearmed": True}
            self._dump()
            return
        self._state = {
            "tripped": False,
            "reason": "",
            "cancel_open": False,
            "rearmed": True,
            "rearmed_by": operator,
            "rearmed_ts": datetime.now(UTC).isoformat(),
            "previous_reason": self.reason,
        }
        self._dump()

    def rearm_if_stale(self) -> None:
        if self.is_tripped:
            raise KillSwitchError("kill switch stays tripped across restarts; requires a manual re-arm")
