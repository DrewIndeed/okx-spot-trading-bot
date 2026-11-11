import json
import time

import pytest

from tradingbot.data.ws import MemoryTransport, OkxPublicWS, decode_frames, encode_frame


def test_frame_roundtrip():
    payload = b'{"hello":1}'
    raw = encode_frame(payload, opcode=0x1, masked=True)
    frames, rest = decode_frames(bytearray(raw))
    assert rest == b"" or rest == bytearray()
    assert len(frames) == 1
    assert frames[0][0] == 0x1
    assert frames[0][1] == payload


def test_subscribe_and_message_with_mock():
    t = MemoryTransport()
    got = []
    client = OkxPublicWS(transport=t, transport_factory=lambda: t, on_message=got.append)
    client.subscribe("candle1H", "BTC-USDT")
    client.connect()
    assert t.connect_count == 1
    sent = [json.loads(x.decode()) for x in t.sent]
    assert sent[0]["op"] == "subscribe"
    assert sent[0]["args"][0] == {"channel": "candle1H", "instId": "BTC-USDT"}
    t.push({"arg": {"channel": "candle1H", "instId": "BTC-USDT"}, "data": [["1", "2"]]})
    msg = client.recv_one(timeout=1)
    assert msg["data"][0][0] == "1"
    assert got and got[0]["data"]


def test_reconnect_resubscribes():
    # factory returns a new MemoryTransport each connect
    transports = []

    def factory():
        t = MemoryTransport()
        transports.append(t)
        return t

    sleeps = []
    client = OkxPublicWS(
        transport_factory=factory,
        sleeper=lambda s: sleeps.append(s),
        backoff_start=0.01,
        backoff_max=0.02,
        ping_interval=100,
    )
    client.subscribe("tickers", "BTC-USDT")

    # First connect works, then recv raises ConnectionError -> reconnect
    # Drive two iterations of the inner loop via a custom pump
    client.connect()
    assert client.transport is transports[0]
    assert any(b"subscribe" in x for x in transports[0].sent)
    # drop
    transports[0].close()
    # next recv_one should reconnect via connect()
    transports[0].fail_connect = True  # old one stays closed
    client._transport = transports[0]
    # connect uses factory because closed
    client.connect()
    assert len(transports) >= 2
    assert any(b"subscribe" in x for x in transports[-1].sent)


def test_public_ws_smoke_if_network_allows():
    """Short real connect to OKX public WS. Skips if the network is unavailable."""
    client = OkxPublicWS()
    try:
        msg = client.run_smoke(inst_id="BTC-USDT", bar="1H", timeout=12.0)
    except Exception as e:
        pytest.skip(f"OKX public WS not reachable: {e}")
    assert isinstance(msg, dict)
    assert msg.get("data") or msg.get("arg")
