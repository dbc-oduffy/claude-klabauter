"""Structured per-run accept-partial list: PARTIAL chunks an EM ruled good enough to release their dependents.

Invariant: an acceptance lives only in a run's record dir (beside ``notes.jsonl``) and expires when
that dir lands. It releases a PARTIAL row only; a BLOCKED or failed row keeps holding. Stdlib only;
spawns no process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from coordinator_core.session.declared_writes import declare_write

ACCEPT_PARTIAL_FILE = "accept_partial.jsonl"
MAX_ENTRIES = 64
REASON_CAP = 500
_CHUNK_RE = re.compile(r"^[A-Za-z0-9_.@+-][A-Za-z0-9_.@+/-]*$")


def validate(raw: object) -> list[dict]:
    """Normalise ``[{chunk, reason}]``; raise ``ValueError`` naming the first bad entry."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("accept_partial must be a list of {chunk, reason}")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"accept_partial holds {len(raw)} entries; the cap is {MAX_ENTRIES}")
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"accept_partial[{i}] is not an object with chunk and reason")
        chunk = entry.get("chunk")
        chunk = chunk.strip() if isinstance(chunk, str) else ""
        if not chunk or not _CHUNK_RE.match(chunk):
            raise ValueError(f"accept_partial[{i}].chunk {entry.get('chunk')!r} is not a chunk id")
        reason = entry.get("reason")
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise ValueError(f"accept_partial[{i}].reason is empty")
        out.append({"chunk": chunk, "reason": reason[:REASON_CAP]})
    return out


def record(dir_: Path, entries: list[dict], at: str) -> Path:
    """Append one ``{chunk, reason, at}`` JSON line per entry to ``dir_/accept_partial.jsonl``."""
    from coordinator_core.atomic_append import append_line

    sink = Path(dir_) / ACCEPT_PARTIAL_FILE
    sink.parent.mkdir(parents=True, exist_ok=True)
    for e in entries:
        line = json.dumps({"chunk": e["chunk"], "reason": e["reason"], "at": at}, ensure_ascii=False)
        append_line(sink, (line + "\n").encode("utf-8"))
    declare_write(sink)
    return sink


def read(record_dirs: Iterable[Path]) -> dict[str, str]:
    """``{chunk: reason}`` across the dirs; later line wins per chunk; never raises."""
    merged: dict[str, str] = {}
    for d in record_dirs:
        try:
            text = (Path(d) / ACCEPT_PARTIAL_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            chunk, reason = obj.get("chunk"), obj.get("reason")
            if isinstance(chunk, str) and chunk and isinstance(reason, str) and reason:
                merged[chunk] = reason
    return merged
