"""The ``{path, sha256, bytes}`` handle contract for an op reply that feeds a later stage.

The engine writes the full reply to a run-scoped sidecar file and returns only the handle; a reader
re-reads the file by path and refuses unless its bytes match the handle. No git, no subprocess.
The sidecar is ASCII JSON, so the Python sha256 and the script-side ``_sha256Ascii`` agree.
Handle paths are repo-relative POSIX strings, contained under ``SIDECAR_ROOTS``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from coordinator_core.ops import _path_guard
from coordinator_core.ops.dispatch_emit import grind_commit_contract as _contract

HANDLE_KEYS = ("path", "sha256", "bytes")
SIDECAR_ROOTS = ("state/warp", "scratch/warp", "scratch")

SIDECAR_CAUSE = "the sidecar changed after the engine wrote it"
_FLAG = "reply-sidecar"


class ReplySidecarError(ValueError):
    """A handle or its sidecar failed verification; the message starts with ``RELAY_CORRUPT_HEAD``."""


def _refuse(reason: str) -> ReplySidecarError:
    return ReplySidecarError(f"{_contract.RELAY_CORRUPT_HEAD}: {reason}")


def _rel_posix(path: object) -> str:
    """The repo-relative POSIX form of a handle path, or a refusal before any filesystem access."""
    if not isinstance(path, str) or not path:
        raise _refuse(f"handle path {path!r} is not a non-empty str")
    if path.startswith("/") or "\\" in path or (len(path) > 1 and path[1] == ":"):
        raise _refuse(f"handle path {path!r} is not repo-relative POSIX")
    if ".." in path.split("/"):
        raise _refuse(f"handle path {path!r} contains '..'")
    return path


def _contain(root: Path, rel: str) -> Path:
    roots = [Path(root) / r for r in SIDECAR_ROOTS]
    resolved = _path_guard.contained_path(Path(root) / rel, roots)
    if resolved is None:
        raise _refuse(f"handle path {rel!r} is outside {list(SIDECAR_ROOTS)}")
    return resolved


def _relative_to_root(root: Path, path: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        return _rel_posix(p.as_posix())
    try:
        return p.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        raise _refuse(f"path {p.as_posix()!r} is not under the repo root") from None


def _handle(rel: str, raw: bytes) -> dict:
    return {"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def write_reply(root: Path, path: Path, payload: Mapping) -> dict:
    """Write ``payload`` as ASCII JSON (indent 2, trailing newline) at ``path`` atomically; return its handle."""
    rel = _relative_to_root(root, path)
    target = _contain(root, rel)
    raw = (json.dumps(payload, indent=2) + "\n").encode("ascii")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return _handle(rel, raw)


def handle_for(root: Path, path: Path) -> dict:
    """The handle over the file's current bytes."""
    rel = _relative_to_root(root, path)
    try:
        raw = _contain(root, rel).read_bytes()
    except OSError as exc:
        raise _refuse(f"sidecar {rel!r} is unreadable: {exc}") from exc
    return _handle(rel, raw)


def _checked_handle(handle: object) -> Mapping:
    if not isinstance(handle, Mapping) or set(handle) != set(HANDLE_KEYS):
        raise _refuse(f"handle must be an object with exactly the keys {list(HANDLE_KEYS)}")
    size = handle["bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise _refuse(f"handle bytes {size!r} is not a non-negative integer")
    return handle


def read_text(root: Path, handle: Mapping) -> str:
    """The sidecar's text when its bytes match ``handle``; else ``ReplySidecarError``, before any decode."""
    h = _checked_handle(handle)
    rel = _rel_posix(h["path"])
    target = _contain(root, rel)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise _refuse(f"sidecar {rel!r} is unreadable: {exc}") from exc
    text = raw.decode("latin-1")
    message = _contract.check_relay(_FLAG, text, str(h["bytes"]), h["sha256"], SIDECAR_CAUSE)
    if message is not None:
        raise ReplySidecarError(message)
    return text


def read_reply(root: Path, handle: Mapping) -> dict:
    """The verified sidecar parsed as JSON."""
    text = read_text(root, handle)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _refuse(f"sidecar {handle['path']!r} is not valid JSON: {exc}") from exc
