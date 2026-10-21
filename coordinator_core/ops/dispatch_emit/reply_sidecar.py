"""The ``{path, sha256, bytes}`` handle contract for an op reply that feeds a later stage.

The engine writes the full reply to a run-scoped sidecar file and returns only the handle; a reader
re-reads the file by path and refuses unless its bytes match the handle. No git, no subprocess.
The sidecar is ASCII JSON, so the Python sha256 and the script-side ``_sha256Ascii`` agree.
Handle paths are repo-relative POSIX strings, contained under ``SIDECAR_ROOTS``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

from coordinator_core.ops import _path_guard
from coordinator_core.ops.dispatch_emit import grind_commit_contract as _contract

HANDLE_KEYS = ("path", "sha256", "bytes")
SIDECAR_ROOTS = ("state/warp", "scratch")

SIDECAR_CAUSE = "the sidecar changed after the engine wrote it"
_FLAG = "reply-sidecar"

OP_REPLY_PAGE = "dispatch.reply_page"
# Bounds each page's JSON-quoted form, so a page relayed inside a JSON reply stays under the 12000-char capture cut.
PAGE_CHARS = 6000
# Sidecar characters per page: base64 of a full page is 4 * PAGE_RAW_CHARS / 3 chars, quotes included within PAGE_CHARS.
PAGE_RAW_CHARS = ((PAGE_CHARS - 2) // 4) * 3

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
    },
}

PAGE_REPLY_SCHEMA = {
    "type": "object",
    "required": ["sha256", "bytes", "page", "pages", "encoding", "chunk", "chunk_sha256"],
    "additionalProperties": False,
    "properties": {
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "bytes": {"type": "integer", "minimum": 0},
        "page": {"type": "integer", "minimum": 0},
        "pages": {"type": "integer", "minimum": 1},
        "encoding": {"const": "base64"},
        "chunk": {"type": "string", "pattern": "^[A-Za-z0-9+/]*={0,2}$"},
        "chunk_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}

VERIFY_REPLY_JS = (
    _contract.RELAY_SHA256_JS
    + """
function _b64DecodeAscii(s) {
  if (typeof s !== 'string' || s.length % 4 !== 0) return null;
  const A = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let out = '';
  for (let i = 0; i < s.length; i += 4) {
    let v = 0, pad = 0;
    for (let j = 0; j < 4; j++) {
      const c = s.charAt(i + j);
      if (c === '=') {
        if (i + 4 !== s.length || j < 2) return null;
        pad++;
        v = v * 64;
      } else {
        if (pad > 0) return null;
        const k = A.indexOf(c);
        if (k < 0) return null;
        v = v * 64 + k;
      }
    }
    const b = [Math.floor(v / 65536) % 256, Math.floor(v / 256) % 256, v % 256];
    for (let n = 0; n < 3 - pad; n++) {
      if (b[n] > 127) return null;
      out += String.fromCharCode(b[n]);
    }
  }
  return out;
}
function _verifyPageChunk(p) {
  if (p === null || typeof p !== 'object') return { fail: 'page reply is not an object' };
  const text = _b64DecodeAscii(p.chunk);
  if (text === null) return { fail: 'page chunk is not base64 of ASCII text' };
  if (_sha256Ascii(text) !== p.chunk_sha256) return { fail: 'page chunk sha256 does not match chunk_sha256' };
  return { text: text };
}
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


def write_reply(root: Path, path: Path, payload: Mapping) -> dict:
    """Write ``canonical_json(payload)`` at ``path`` atomically; return its handle.

    Trap: pages relay as base64, so the reader compares bytes; ``canonical_json`` is the one form
    the handle covers and a re-indented copy is refused.
    """
    rel = _relative_to_root(root, path)
    target = _contain(root, rel)
    raw = canonical_json(payload).encode("ascii")
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


def _pages(text: str) -> list[str]:
    return [text[i : i + PAGE_RAW_CHARS] for i in range(0, len(text), PAGE_RAW_CHARS)] or [""]


def page(text: str, index: int) -> tuple[str, int]:
    """``(raw slice, pages)``; every slice but the last is ``PAGE_RAW_CHARS`` long. ``IndexError`` past the end."""
    pages = _pages(text)
    if not 0 <= index < len(pages):
        raise IndexError(f"page {index} is outside 0..{len(pages) - 1}")
    return pages[index], len(pages)


def page_reply(handle: Mapping, text: str, index: int) -> dict:
    """The success shape of ``dispatch.reply_page``: the page as base64 plus the sha256 of its decoded text.

    The reader decodes and checks each chunk, joins the decoded text, and checks it against the handle.
    """
    raw, pages = page(text, index)
    data = raw.encode("ascii")
    return {
        "sha256": handle["sha256"],
        "bytes": handle["bytes"],
        "page": index,
        "pages": pages,
        "encoding": "base64",
        "chunk": base64.b64encode(data).decode("ascii"),
        "chunk_sha256": hashlib.sha256(data).hexdigest(),
    }


def read_reply(root: Path, handle: Mapping) -> dict:
    """The verified sidecar parsed as JSON."""
    text = read_text(root, handle)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _refuse(f"sidecar {handle['path']!r} is not valid JSON: {exc}") from exc
