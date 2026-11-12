"""Portable path resolution (v3-local). Not a frozen file.

Order for each input: env override -> repo copy (config/v3/) -> old box path.
The official box worktree (/workspace/trading-bot-v3) keeps its old behaviour:
repo root == V3 root, artifacts under /workspace/sprint/artifacts/spec_v3.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_V3 = REPO_ROOT / "config" / "v3"
OFFICIAL_V3_ROOT = Path("/workspace/trading-bot-v3")

BOX_FAVORITES = Path("/workspace/crypto/favorites.txt")
BOX_V3_FREEZE = Path("/workspace/sprint/artifacts/spec_v3/freeze.json")
BOX_V3_BRIEF = Path("/workspace/sprint/strategy-v3-brief.md")
BOX_V3_ARTIFACTS = Path("/workspace/sprint/artifacts/spec_v3")


def _env(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else None


def _pick(env: str, repo_copy: Path, box: Path) -> Path:
    e = _env(env)
    if e is not None:
        return e
    return repo_copy if repo_copy.exists() else box


def favorites_path(explicit: str | Path | None = None) -> Path:
    """TB_FAVORITES > explicit path (if it exists) > config/v3/favorites.txt > box path."""
    e = _env("TB_FAVORITES")
    if e is not None:
        return e
    if explicit:
        p = Path(explicit)
        if not p.is_absolute() and not p.as_posix().startswith("/"):
            p = REPO_ROOT / p
        if p.exists():
            return p
    repo = CONFIG_V3 / "favorites.txt"
    if repo.exists():
        return repo
    return Path(explicit) if explicit else BOX_FAVORITES


def v3_freeze_path() -> Path:
    return _pick("TB_V3_FREEZE", CONFIG_V3 / "freeze.json", BOX_V3_FREEZE)


def v3_brief_path() -> Path:
    return _pick("TB_V3_BRIEF", CONFIG_V3 / "strategy-v3-brief.md", BOX_V3_BRIEF)


def v3_root() -> Path:
    """Where v3 state/, logs/, data/ and RUNNING.lock live. TB_V3_ROOT > repo root."""
    return _env("TB_V3_ROOT") or REPO_ROOT


def is_official_box_root(root: Path | None = None) -> bool:
    r = root or v3_root()
    return r.as_posix() == OFFICIAL_V3_ROOT.as_posix()


def v3_artifact_root() -> Path:
    """TB_V3_ARTIFACTS > official box artifacts (official worktree only) > <v3_root>/state/artifacts/spec_v3."""
    e = _env("TB_V3_ARTIFACTS")
    if e is not None:
        return e
    if is_official_box_root():
        return BOX_V3_ARTIFACTS
    return v3_root() / "state" / "artifacts" / "spec_v3"


def frozen_code_hash(code_root: Path | None = None) -> str:
    """Same digest as frozen v3_protocol.sha256_paths(), but portable.

    The frozen definition hashes, sorted by path string, the POSIX path label
    '/workspace/trading-bot-v3/<rel>' followed by the file bytes. We keep the
    exact label strings (so the digest is unchanged) and read the bytes from the
    code actually imported (this repo), not from the box worktree. Bytes are
    LF-normalized (CRLF -> LF) so a Windows autocrlf checkout of unchanged code
    reproduces the official digest; the frozen files are LF, so the value is identical.
    """
    import hashlib

    from tradingbot.eval import v3_protocol as P

    root = Path(code_root) if code_root is not None else REPO_ROOT
    h = hashlib.sha256()
    for p in sorted(P.V3_CODE_PATHS, key=lambda x: Path(x).as_posix()):
        label = Path(p).as_posix()
        rel = label[len(OFFICIAL_V3_ROOT.as_posix()) + 1 :]
        local = root.joinpath(*rel.split("/"))
        h.update(label.encode())
        h.update(local.read_bytes().replace(b"\r\n", b"\n") if local.exists() else b"MISSING")
    return h.hexdigest()
