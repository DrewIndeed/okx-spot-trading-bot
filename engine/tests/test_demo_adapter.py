from __future__ import annotations

import json
from pathlib import Path

import httpx

from okxspot.cli import main
from okxspot.demo import DEMO_HEADER, DEMO_HEADER_VALUE, demo_keys_from_env, missing_demo_keys_message
from okxspot.exchange.okx_client import OkxClient
from okxspot.safety import LIVE_CONFIRM_VALUE
from tests.conftest import write_config


class _Limiter:
    def acquire(self) -> None:
        return None


def test_simulated_client_sends_demo_header_on_private_requests() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.url.path.endswith("/public/time"):
            return httpx.Response(200, json={"code": "0", "data": [{"ts": "1700000000000"}]})
        if request.url.path.endswith("/trade/order"):
            return httpx.Response(
                200,
                json={"code": "0", "data": [{"ordId": "1", "clOrdId": "c" + "a" * 31, "sCode": "0"}]},
            )
        return httpx.Response(404, json={"code": "404", "msg": request.url.path})

    client = OkxClient(
        api_key="test-key",
        secret="test-secret",
        passphrase="test-pass",
        transport=httpx.MockTransport(handler),
        limiter=_Limiter(),
        simulated=True,
    )
    client.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
    client.request(
        "POST",
        "/api/v5/trade/order",
        body={"instId": "BTC-USDT", "tdMode": "cash", "side": "buy", "ordType": "market", "sz": "0.01"},
        auth=True,
        idempotent=False,
    )
    client.close()
    assert any(h.get(DEMO_HEADER) == DEMO_HEADER_VALUE or h.get(DEMO_HEADER.lower()) == DEMO_HEADER_VALUE for h in seen[1:])
    # live/non-simulated client must not send the header
    seen.clear()
    live = OkxClient(
        api_key="test-key",
        secret="test-secret",
        passphrase="test-pass",
        transport=httpx.MockTransport(handler),
        limiter=_Limiter(),
        simulated=False,
    )
    live.request("GET", "/api/v5/public/time", auth=False, idempotent=True)
    live.close()
    assert all(
        h.get(DEMO_HEADER) is None and h.get(DEMO_HEADER.lower()) is None for h in seen
    )


def test_demo_dry_run_exits_cleanly_without_keys(tmp_path: Path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OKX_DEMO_API_KEY", raising=False)
    monkeypatch.delenv("OKX_DEMO_API_SECRET", raising=False)
    monkeypatch.delenv("OKX_DEMO_API_PASSPHRASE", raising=False)
    monkeypatch.delenv("OKX_API_KEY", raising=False)
    cfg = write_config(tmp_path)
    code = main(["--log-level", "ERROR", "demo-dry-run", "--config", str(cfg)], env={})
    captured = capsys.readouterr()
    assert code == 0
    assert "demo key is needed" in captured.out.lower() or "demo key is needed" in captured.err.lower()
    assert missing_demo_keys_message() in (captured.out + captured.err)
    assert demo_keys_from_env({}) is None


def test_demo_keys_are_redacted_and_env_example_is_empty() -> None:
    keys = demo_keys_from_env(
        {
            "OKX_DEMO_API_KEY": "demo-key",
            "OKX_DEMO_API_SECRET": "demo-secret",
            "OKX_DEMO_API_PASSPHRASE": "demo-pass",
        }
    )
    assert keys is not None
    assert "demo-secret" not in repr(keys)
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "OKX_DEMO_API_KEY=" in text
    assert "OKX_DEMO_API_SECRET=" in text
    assert "OKX_DEMO_API_PASSPHRASE=" in text
    for line in text.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            _, value = line.split("=", 1)
            assert value.strip() == ""
            assert LIVE_CONFIRM_VALUE not in value
