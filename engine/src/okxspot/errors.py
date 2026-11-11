from __future__ import annotations


class OkxSpotError(Exception):
    """Base error for the engine."""


class LiveGuardError(OkxSpotError):
    """Live mode refused by a safety check. No order was sent."""


class StateError(OkxSpotError):
    """Persisted state does not match the current config."""


class OkxApiError(OkxSpotError):
    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        self.code = str(code)
        self.message = message
        self.http_status = http_status
        super().__init__(f"{self.code}: {message}")


class AmbiguousOrderError(OkxSpotError):
    """A private order request may or may not have reached the exchange."""
