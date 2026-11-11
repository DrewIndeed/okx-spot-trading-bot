from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from okxspot.errors import AmbiguousOrderError, LiveGuardError, OkxApiError
from okxspot.exchange.rate_limit import TokenBucket, backoff_seconds
from okxspot.exchange.signing import build_request_path, format_okx_timestamp, okx_sign
from okxspot.demo import DEMO_HEADER, DEMO_HEADER_VALUE
from okxspot.redact import register_secret
from okxspot.safety import NETWORK_DISABLE_ENV

# Business codes that mean "try again", the request was not accepted.
_RETRY_CODES = {"50001", "50011", "50013"}
_NOT_FOUND_CODES = {"51603"}


class OkxClient:
    """OKX v5 REST client: signed private requests, clock offset, limit, retry.

    Order POSTs are not retried on timeout or HTTP 5xx. The caller looks the
    client order id up and only then submits again. Rate-limit rejects (the
    request never entered the matching engine) are safe to retry.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://www.okx.com",
        api_key: str | None = None,
        secret: str | None = None,
        passphrase: str | None = None,
        timeout_s: float = 10,
        max_retries: int = 5,
        rate_limit_per_sec: float = 5,
        transport: httpx.BaseTransport | None = None,
        now_ms: Callable[[], int] | None = None,
        sleeper: Callable[[float], None] | None = None,
        limiter: TokenBucket | None = None,
        simulated: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.secret = secret or ""
        self.passphrase = passphrase or ""
        self.max_retries = max_retries
        self._offset_ms = 0
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleeper = sleeper or time.sleep
        self._mock = transport is not None
        self.limiter = limiter or TokenBucket(rate_limit_per_sec)
        self.simulated = bool(simulated)
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout_s, transport=transport)
        register_secret(self.api_key)
        register_secret(self.secret)
        register_secret(self.passphrase)

    def __repr__(self) -> str:
        return (
            f"OkxClient(base_url={self.base_url!r}, api_key={'set' if self.api_key else 'unset'}, "
            "secret=***, passphrase=***, simulated=" + str(self.simulated) + ")"
        )

    def __str__(self) -> str:
        return self.__repr__()

    @property
    def uses_mock_transport(self) -> bool:
        return self._mock

    def close(self) -> None:
        self._http.close()

    def timestamp(self) -> str:
        return format_okx_timestamp(self._now_ms() + self._offset_ms)

    def sync_clock(self) -> int:
        before = self._now_ms()
        payload = self.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
        after = self._now_ms()
        data = payload.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise OkxApiError("time", "empty time payload")
        server = int(str(data[0]["ts"]))
        mid = (before + after) // 2
        self._offset_ms = server - mid
        return self._offset_ms

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        auth: bool = False,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        self._guard_network()
        import json

        body_text = "" if body is None else json.dumps(body, separators=(",", ":"))
        request_path = build_request_path(path, params)
        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            headers = self._headers(auth, method, request_path, body_text)
            try:
                response = self._http.request(
                    method,
                    request_path,
                    headers=headers,
                    content=body_text if body is not None else None,
                )
            except httpx.TransportError as exc:
                if not idempotent:
                    raise AmbiguousOrderError(str(exc)) from exc
                if attempt >= self.max_retries:
                    raise OkxApiError("transport", str(exc)) from exc
                self._sleeper(backoff_seconds(attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if response.status_code >= 500 and not idempotent:
                    raise AmbiguousOrderError(
                        f"HTTP {response.status_code} on non-idempotent {method} {path}"
                    )
                if attempt >= self.max_retries:
                    raise OkxApiError(str(response.status_code), response.text, response.status_code)
                self._sleeper(backoff_seconds(attempt))
                continue
            if response.status_code >= 400:
                raise OkxApiError(str(response.status_code), response.text, response.status_code)
            payload = response.json()
            if not isinstance(payload, dict):
                raise OkxApiError("payload", "OKX response was not an object")
            code = str(payload.get("code", "0"))
            if code in _NOT_FOUND_CODES:
                return payload
            if code in _RETRY_CODES:
                if attempt >= self.max_retries:
                    raise OkxApiError(code, str(payload.get("msg") or "retry exhausted"))
                self._sleeper(backoff_seconds(attempt))
                continue
            if code not in {"0", ""}:
                raise OkxApiError(code, str(payload.get("msg") or "okx error"))
            return payload

    def _headers(self, auth: bool, method: str, path: str, body: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "okxspot/0.1"}
        if self.simulated:
            headers[DEMO_HEADER] = DEMO_HEADER_VALUE
        if not auth:
            return headers
        if not self.api_key or not self.secret or not self.passphrase:
            raise LiveGuardError("private OKX request without API credentials")
        stamp = self.timestamp()
        headers.update(
            {
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": okx_sign(self.secret, stamp, method, path, body),
                "OK-ACCESS-TIMESTAMP": stamp,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
            }
        )
        return headers

    def _guard_network(self) -> None:
        if os.environ.get(NETWORK_DISABLE_ENV) == "1" and not self._mock:
            raise RuntimeError(
                "network disabled (OKXSPOT_DISABLE_NETWORK=1) and this client has no mock transport"
            )
