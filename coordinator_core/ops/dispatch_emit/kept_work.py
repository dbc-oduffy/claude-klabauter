"""Kept work: the finished product and reviewed plan a halted run leaves, fingerprinted for a refire.

Invariant: ``kept.json`` lives in the run record dir beside ``notes.jsonl``. A kept chunk or plan
gate is honoured only while every recorded fingerprint still equals the bytes on disk. Stdlib only;
spawns no process.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.session.declared_writes import declare_write

KEPT_FILE = "kept.json"
HALT_RECORD_ONLY = "halt-record-only"


def blob_sha(data: bytes) -> str:
    """Git's blob id: sha1 over ``blob <len>\\0`` plus the bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fingerprint(root: Path, rel: str) -> Optional[str]:
    """``blob_sha`` of ``root/rel`` read in binary mode, or None when absent or unreadable."""
    try:
        return blob_sha((Path(root) / rel).read_bytes())
    except OSError:
        return None


def kept_chunks(root: Path, owner: Mapping[str, str], withheld: Mapping[str, str]) -> dict:
    """``{chunk: {"paths": {rel: fingerprint}}}`` for each chunk all of whose withheld paths carry
    ``halt-record-only``; ``owner`` maps path to chunk, ``withheld`` maps path to reason."""
    by_chunk: dict[str, dict[str, str]] = {}
    for path, reason in withheld.items():
        chunk = owner.get(path)
        if chunk:
            by_chunk.setdefault(chunk, {})[path] = reason
    out: dict[str, dict] = {}
    for chunk, paths in by_chunk.items():
        if all(r == HALT_RECORD_ONLY for r in paths.values()):
            prints = {p: fingerprint(root, p) for p in paths}
            if all(v is not None for v in prints.values()):
                out[chunk] = {"paths": prints}
    return out


def _pulled(blitz: object, plan: str) -> Optional[dict]:
    if not isinstance(blitz, Mapping):
        return None
    regate = blitz.get("regate")
    for holder in (regate, blitz):
        pulled = holder.get("pulled") if isinstance(holder, Mapping) else None
        if isinstance(pulled, list):
            for entry in pulled:
                if isinstance(entry, Mapping) and entry.get("planPath") == plan:
                    return dict(entry)
    return None


def plan_gate(halted: Mapping, root: Path) -> Optional[dict]:
    """``{plan, plan_sha256, reviews, coverage}`` for a refusal halt whose blitz pulled ``plan``; else None."""
    if halted.get("halted") != "refusal":
        return None
    plan = halted.get("plan")
    if not isinstance(plan, str) or not plan:
        return None
    entry = _pulled(halted.get("blitz"), plan)
    if entry is None:
        return None
    try:
        digest = hashlib.sha256((Path(root) / plan).read_bytes()).hexdigest()
    except OSError:
        return None
    return {
        "plan": plan,
        "plan_sha256": digest,
        "reviews": entry.get("reviews"),
        "coverage": entry.get("coverage"),
    }


def write(record_dir: Path, chunks: Mapping, plan_gate: Optional[Mapping]) -> Optional[Path]:
    """Write ``kept.json`` (UTF-8, LF, sorted keys) when either part is non-empty; return its path or None."""
    if not chunks and not plan_gate:
        return None
    path = Path(record_dir) / KEPT_FILE
    body = json.dumps({"chunks": dict(chunks), "plan_gate": plan_gate or None}, sort_keys=True, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body + "\n")
    except OSError:
        return None
    declare_write(path)
    return path


def read(record_dir: Path) -> dict:
    """``{"chunks": {...}, "plan_gate": {...} | None}``; empty parts when absent or malformed. Never raises."""
    empty: dict = {"chunks": {}, "plan_gate": None}
    try:
        data = json.loads((Path(record_dir) / KEPT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    chunks = data.get("chunks")
    gate = data.get("plan_gate")
    return {
        "chunks": chunks if isinstance(chunks, dict) else {},
        "plan_gate": gate if isinstance(gate, dict) else None,
    }


def still_kept(root: Path, entry: object, notes: Sequence[str], row_id: str) -> bool:
    """True while every recorded path still fingerprints the same and no note names the row or a path."""
    paths = entry.get("paths") if isinstance(entry, Mapping) else None
    if not isinstance(paths, Mapping) or not paths:
        return False
    for rel, want in paths.items():
        if not isinstance(want, str) or fingerprint(root, rel) != want:
            return False
    token = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(row_id) + r"(?![A-Za-z0-9_])")
    for note in notes:
        if token.search(note) or any(rel in note for rel in paths):
            return False
    return True
