from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from okxspot.errors import OkxSpotError


class KillSwitchError(OkxSpotError):
    """Kill-switch refused an automatic re-arm or an invalid state change."""


class KillSwitch:
    """File-backed kill switch. Missing or corrupt files are treated as tripped.

    Writes are atomic (tmp → fsync → rename). Only a manual re-arm clears it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        on_cancel_open: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self._on_cancel_open = on_cancel_open
        self._cancelled = False
        self._state: dict[str, object] = {
            "tripped": True,
            "reason": "missing",
            "cancel_open": False,
            "source": "missing",
        }
        self._load()

    def reload(self) -> None:
        """Re-read the file so a CLI trip is visible to a running process."""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._state = {
                "tripped": True,
                "reason": self._state.get("reason") or "missing",
                "cancel_open": bool(self._state.get("cancel_open")),
                "source": "missing",
            }
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeError):
            self._state = {
                "tripped": True,
                "reason": "corrupt",
                "cancel_open": False,
                "source": "corrupt",
            }
            return
        if not isinstance(data, dict) or not isinstance(data.get("tripped"), bool):
            self._state = {
                "tripped": True,
                "reason": "invalid",
                "cancel_open": False,
                "source": "corrupt",
            }
            return
        self._state = dict(data)

    def _dump(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state, indent=2, sort_keys=True) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            # Best-effort fsync of the directory so the rename itself is durable.
            try:
                dir_fd = os.open(str(self.path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

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
        if cancel_open and not self._cancelled and self._on_cancel_open is not None:
            self._on_cancel_open()
            self._cancelled = True

    def rearm(self, *, operator: str = "manual") -> None:
        previous = self.reason
        self._state = {
            "tripped": False,
            "reason": "",
            "cancel_open": False,
            "rearmed": True,
            "rearmed_by": operator,
            "rearmed_ts": datetime.now(UTC).isoformat(),
            "previous_reason": previous,
            "source": "rearm",
        }
        self._cancelled = False
        self._dump()

    def rearm_if_stale(self) -> None:
        if self.is_tripped:
            raise KillSwitchError("kill switch stays tripped across restarts; requires a manual re-arm")
