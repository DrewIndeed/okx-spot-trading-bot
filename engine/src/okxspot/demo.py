from __future__ import annotations

from collections.abc import Mapping

from okxspot.safety import OkxKeys

DEMO_HEADER = "x-simulated-trading"
DEMO_HEADER_VALUE = "1"

DEMO_KEY_ENV = "OKX_DEMO_API_KEY"
DEMO_SECRET_ENV = "OKX_DEMO_API_SECRET"
DEMO_PASSPHRASE_ENV = "OKX_DEMO_API_PASSPHRASE"


def missing_demo_keys_message() -> str:
    return (
        "A demo key is needed. Create an OKX demo-trading API key with read+trade "
        "and no withdraw, then enter OKX_DEMO_API_KEY, OKX_DEMO_API_SECRET, and "
        "OKX_DEMO_API_PASSPHRASE through the secret form (never chat). This command "
        "did not place any order."
    )


def demo_keys_from_env(env: Mapping[str, str]) -> OkxKeys | None:
    key = env.get(DEMO_KEY_ENV, "").strip()
    secret = env.get(DEMO_SECRET_ENV, "").strip()
    passphrase = env.get(DEMO_PASSPHRASE_ENV, "").strip()
    if not key or not secret or not passphrase:
        return None
    return OkxKeys(key, secret, passphrase)
