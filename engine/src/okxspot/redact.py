from __future__ import annotations

import re

_SECRETS: list[str] = []
_BOT_RE = re.compile(r"(/bot)[^/\s]+")
_AUTH_RE = re.compile(r"(passphrase|secret|token|api[_-]?key|authorization)([\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}]+)", re.I)


def register_secret(value: str | None) -> None:
    text = (value or "").strip()
    if len(text) >= 4 and text not in _SECRETS:
        _SECRETS.append(text)


def redact_text(text: str) -> str:
    out = str(text)
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret and secret in out:
            out = out.replace(secret, "***")
    out = _BOT_RE.sub(r"\1***", out)
    out = _AUTH_RE.sub(r"\1\2***", out)
    return out
