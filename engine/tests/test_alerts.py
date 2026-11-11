from __future__ import annotations

import httpx

from okxspot.alerts import LogAlerter, TelegramAlerter


def test_telegram_swallows_transport_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    TelegramAlerter("test-token", "1", client=client).send("info", "hello")
    client.close()
    LogAlerter().send("info", "hello")


def test_telegram_posts_without_logging_token(capsys) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    TelegramAlerter("test-token", "42", client=client).send("info", "signal")
    client.close()
    assert seen and seen[0].endswith("/sendMessage")
    assert "test-token" not in capsys.readouterr().out
