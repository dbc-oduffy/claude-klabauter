"""agy spool and read-receipt store: append a message to an agy conversation's spool, record and look up its read receipt.

Spool line format and lock are the agy leg's own (`unimatrix-agy-leg.py`: `nt_spool_lock`, `SPOOL_MAX_BYTES`),
loaded as a sibling script so the spool drains through the same hook path. Importing this module does no I/O.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_BIN = Path(__file__).resolve().parent.parent
_CONV_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_LEG = None

# Mirrors unimatrix-agy-hook-pretool.py RUNG_TTL_SECS; a test pins the two equal.
RECEIPT_TTL_SECS = 6 * 3600


def _leg() -> Any:
    """The agy leg script as a module, executed once per process."""
    global _LEG
    if _LEG is None:
        spec = importlib.util.spec_from_file_location("agy_spool_leg_sibling", _BIN / "unimatrix-agy-leg.py")
        if spec is None or spec.loader is None:
            raise ImportError("unimatrix-agy-leg.py not loadable beside _unimatrix")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _LEG = mod
    return _LEG


def _conv_ok(conv) -> str:
    if not isinstance(conv, str) or not _CONV_RE.fullmatch(conv) or conv in (".", ".."):
        raise ValueError(f"unsafe conversation id: {conv!r}")
    return conv


def spool_path(state, conv) -> Path:
    """`<state>/<conv>.queue.jsonl`, the spool the hook's claim_spool drains."""
    return Path(state) / f"{_conv_ok(conv)}.queue.jsonl"


def receipts_path(state, conv) -> Path:
    """`<state>/<conv>.receipts.jsonl`, the per-conversation read-receipt log."""
    return Path(state) / f"{_conv_ok(conv)}.receipts.jsonl"


@contextlib.contextmanager
def _locked(path) -> Generator[None, None, None]:
    """Exclusive lock on `path`: the leg's msvcrt sidecar lock on nt, flock on a sidecar `<path>.lock` elsewhere."""
    if fcntl is None:
        with _leg().nt_spool_lock(str(path)):
            yield
        return
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[attr-defined]
        yield
    finally:
        os.close(fd)


def spool_append(state, conv, text, sender) -> str:
    """Append one `{"id","text","from"}` line to the conversation's spool and return the uuid4-hex id.

    Raises OSError("spool full") when the spool already holds SPOOL_MAX_BYTES, ValueError for an unsafe conv,
    TimeoutError when another holder keeps the nt lock."""
    path = spool_path(state, conv)
    msg_id = uuid.uuid4().hex
    line = (json.dumps({"id": msg_id, "text": text, "from": sender}) + "\n").encode()
    max_bytes = _leg().SPOOL_MAX_BYTES
    Path(state).mkdir(parents=True, exist_ok=True)
    with _locked(path):
        try:
            full = path.stat().st_size + len(line) > max_bytes
        except OSError:
            full = False
        if full:
            raise OSError("spool full")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    return msg_id


def ensure_pending_record(state, conv, name=None, cwd=None) -> bool:
    """Write `<state>/<conv>.json` as a pending leg record when none exists; True when it wrote one.

    The record is what the PreInvocation hook's claim_spool reads before it drains a spool. An existing
    record (pending or live) is never overwritten. `name`/`cwd` are omitted from the record when falsy."""
    path = Path(state) / f"{_conv_ok(conv)}.json"
    rec: dict = {"conversation": conv, "mode": "pending", "spooled": True}
    if name:
        rec["name"] = name
    if cwd:
        rec["cwd"] = cwd
    Path(state).mkdir(parents=True, exist_ok=True)
    with _locked(path):
        if path.exists():
            return False
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_bytes(json.dumps(rec).encode())
        os.replace(tmp, path)
    return True


def _iso(read_at) -> str:
    if read_at is None:
        read_at = datetime.now(timezone.utc)
    if isinstance(read_at, datetime):
        if read_at.tzinfo is None:
            read_at = read_at.replace(tzinfo=timezone.utc)
        return read_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(read_at)


def _epoch(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _rows(path) -> list[dict]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    rows = []
    for raw in data.splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_receipt(state, conv, msg_id, read_at=None) -> None:
    """Append `{"id","read_at"}` (ISO-8601 UTC) to the conversation's receipts file.

    Rows older than RECEIPT_TTL_SECS are dropped in the same locked rewrite, so the file stays bounded."""
    path = receipts_path(state, conv)
    row = {"id": msg_id, "read_at": _iso(read_at)}
    cutoff = time.time() - RECEIPT_TTL_SECS
    Path(state).mkdir(parents=True, exist_ok=True)
    with _locked(path):
        kept = [r for r in _rows(path) if (_epoch(r.get("read_at")) or cutoff) >= cutoff]
        kept.append(row)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_bytes("".join(json.dumps(r) + "\n" for r in kept).encode())
        os.replace(tmp, path)


def read_receipt(state, msg_id) -> dict | None:
    """The newest `{"id","read_at"}` row for `msg_id` across the state dir's receipts files, or None."""
    found = None
    try:
        files = sorted(Path(state).glob("*.receipts.jsonl"))
    except OSError:
        return None
    for path in files:
        with _locked(path):
            for r in _rows(path):
                if r.get("id") == msg_id:
                    found = r
    return found
