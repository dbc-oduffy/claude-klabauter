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
import re
from pathlib import Path
from typing import Mapping

from coordinator_core.ops import _path_guard
from coordinator_core.ops.dispatch_emit import grind_commit_contract as _contract
from coordinator_core.session import record_homes

HANDLE_KEYS = ("path", "sha256", "bytes")
SIDECAR_ROOTS = (Path(record_homes.home_dir("", "warp")).as_posix(), "scratch")

SIDECAR_CAUSE = "the sidecar changed after the engine wrote it"
_FLAG = "reply-sidecar"

HANDLE_SCHEMA = {
    "type": "object",
    "required": list(HANDLE_KEYS),
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string"},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "bytes": {"type": "integer", "minimum": 0},
    },
}

# No oneOf: one object with optional keys, so a validator that ignores oneOf still binds it.
STAGE_REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "manifest_handle": HANDLE_SCHEMA,
        "error": {"type": "string"},
        "owed_em_ids": {"type": "array", "items": {"type": "string"}},
        "owed_plan": {"type": ["string", "null"]},
        # Literal ask_contract.STOP_AFTER_PLAN, pinned equal by test_reply_sidecar.
        "parked": {"const": "plan"},
        "plan_path": {"type": ["string", "null"]},
    },
}

VERIFY_REPLY_JS = (
    _contract.RELAY_SHA256_JS
    + """
function _verifyReplyText(text, handle) {
  if (typeof text !== 'string') return 'reply text is not a string';
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) > 127) return 'reply text is not ASCII';
  }
  if (text.length !== handle.bytes) return 'reply text is ' + text.length + ' bytes, handle says ' + handle.bytes;
  if (_sha256Ascii(text) !== handle.sha256) return 'reply text sha256 does not match the handle';
  try { JSON.parse(text); } catch (_e) { return 'reply text is not valid JSON'; }
  return null;
}
"""
)


READER_ROOT = "scratch/stage-readers"
# The Workflow runner's script-size limit; not ours to raise.
READER_MAX_BYTES = 524288

# Trap: depends on _verifyReplyText, so VERIFY_REPLY_JS must be emitted ahead of this block.
READ_STAGE_SIDECAR_JS = """
async function _readStageSidecar(h, root) {
  if (h === null || typeof h !== 'object') return { fail: 'handle is not an object' };
  if (typeof h.path !== 'string' || h.path === '') return { fail: 'handle path is not a non-empty string' };
  if (typeof h.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(h.sha256)) return { fail: 'handle sha256 is not 64 lowercase hex' };
  if (typeof h.bytes !== 'number' || !Number.isInteger(h.bytes) || h.bytes < 0) return { fail: 'handle bytes is not a non-negative integer' };
  const reader = String(root) + '/' + 'scratch/stage-readers/' + h.sha256 + '.js';
  let text;
  try {
    text = await workflow({ scriptPath: reader });
  } catch (e) {
    return { fail: 'reader ' + reader + ' did not run: ' + String(e && e.message ? e.message : e) };
  }
  const bad = _verifyReplyText(text, h);
  if (bad !== null) return { fail: bad };
  try {
    return { manifest: JSON.parse(text) };
  } catch (e) {
    return { fail: 'reply text is not valid JSON' };
  }
}
"""


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


def canonical_json(payload: object) -> str:
    """Key-sorted, whitespace-free, ASCII-escaped JSON: the one form the handle's sha256 and bytes cover."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _handle(rel: str, raw: bytes) -> dict:
    return {"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def reader_rel(sha256: str) -> str:
    """``scratch/stage-readers/<sha256>.js``; ``ReplySidecarError`` unless ``sha256`` is 64 lowercase hex."""
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise _refuse(f"reader sha256 {sha256!r} is not 64 lowercase hex")
    return f"{READER_ROOT}/{sha256}.js"


READER_META = "export const meta = { name: 'stage-reply-reader', description: 'Returns one stage reply' }\n"


def reader_script(text: str) -> str:
    """The whole Workflow script that returns ``text``; ASCII-escaped so it is a valid JS string literal.

    The runner refuses a script whose first statement is not the meta block
    (docs/research/spike-verdicts/2026-10-21-workflow-scriptpath-reader.md)."""
    return READER_META + "return " + json.dumps(text, ensure_ascii=True) + ";\n"


def _reader_body(raw: bytes) -> bytes:
    """The reader script bytes for ``raw``; ``ReplySidecarError`` when they exceed ``READER_MAX_BYTES``."""
    body = reader_script(raw.decode("ascii")).encode("ascii")
    if len(body) > READER_MAX_BYTES:
        raise _refuse(f"reader script is {len(body)} bytes, over the Workflow runner's {READER_MAX_BYTES}-byte cap")
    return body


def _ensure_reader(root: Path, raw: bytes, body: bytes) -> None:
    """Leave the content-addressed reader ``body`` for ``raw`` under ``root``; no write when it is already identical."""
    target = _contain(root, reader_rel(hashlib.sha256(raw).hexdigest()))
    try:
        if target.read_bytes() == body:
            return
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(body)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def write_reply(root: Path, path: Path, payload: Mapping) -> dict:
    """Write ``canonical_json(payload)`` at ``path`` atomically; return its handle.

    Trap: ``canonical_json`` is the one form the handle covers and a re-indented copy is refused.
    Refuses before writing anything when the reader script would exceed ``READER_MAX_BYTES``.
    """
    rel = _relative_to_root(root, path)
    target = _contain(root, rel)
    raw = canonical_json(payload).encode("ascii")
    body = _reader_body(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    _ensure_reader(root, raw, body)
    return _handle(rel, raw)


def handle_for(root: Path, path: Path) -> dict:
    """The handle over the file's current bytes."""
    rel = _relative_to_root(root, path)
    try:
        raw = _contain(root, rel).read_bytes()
    except OSError as exc:
        raise _refuse(f"sidecar {rel!r} is unreadable: {exc}") from exc
    try:
        _ensure_reader(root, raw, _reader_body(raw))
    except UnicodeDecodeError:
        pass
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
