"""Structured per-run chunk credits: stranded chunks an EM ruled complete, with evidence.

Invariant: a credit lives only in a run's record dir (beside ``notes.jsonl``) and expires when
that dir lands with the commit that honoured it. Stdlib only; spawns no process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from coordinator_core.ops.dispatch_emit.credit_gate import BOOKKEEPING_CHUNK_IDS
from coordinator_core.session.declared_writes import declare_write

CREDIT_FILE = "credit.jsonl"
MAX_ENTRIES = 64
EVIDENCE_CAP = 500
_CHUNK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate(raw: object) -> list[dict]:
    """Normalise ``[{chunk, evidence}]``; raise ``ValueError`` naming the first bad entry."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError("credit_chunks must be a list of {chunk, evidence}")
    if len(raw) > MAX_ENTRIES:
        raise ValueError(f"credit_chunks holds {len(raw)} entries; the cap is {MAX_ENTRIES}")
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"credit_chunks[{i}] is not an object with chunk and evidence")
        chunk = entry.get("chunk")
        if not isinstance(chunk, str) or not _CHUNK_RE.match(chunk):
            raise ValueError(f"credit_chunks[{i}].chunk {chunk!r} is not a chunk id")
        if chunk in BOOKKEEPING_CHUNK_IDS:
            raise ValueError(f"credit_chunks[{i}].chunk {chunk!r} is a bookkeeping chunk")
        evidence = entry.get("evidence")
        evidence = evidence.strip() if isinstance(evidence, str) else ""
        if not evidence:
            raise ValueError(f"credit_chunks[{i}].evidence is empty")
        out.append({"chunk": chunk, "evidence": evidence[:EVIDENCE_CAP]})
    return out


def record(dir_: Path, entries: list[dict], at: str) -> Path:
    """Append one ``{chunk, evidence, at}`` JSON line per entry to ``dir_/credit.jsonl``."""
    from coordinator_core.atomic_append import append_line

    sink = Path(dir_) / CREDIT_FILE
    sink.parent.mkdir(parents=True, exist_ok=True)
    for e in entries:
        line = json.dumps({"chunk": e["chunk"], "evidence": e["evidence"], "at": at}, ensure_ascii=False)
        append_line(sink, (line + "\n").encode("utf-8"))
    declare_write(sink)
    return sink


def read(record_dirs: Iterable[Path]) -> dict[str, str]:
    """``{chunk: evidence}`` across the dirs; later line wins per chunk; never raises."""
    merged: dict[str, str] = {}
    for d in record_dirs:
        try:
            text = (Path(d) / CREDIT_FILE).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            chunk, evidence = obj.get("chunk"), obj.get("evidence")
            if isinstance(chunk, str) and chunk and isinstance(evidence, str) and evidence:
                merged[chunk] = evidence
    return merged
