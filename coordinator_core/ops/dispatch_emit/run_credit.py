"""Structured per-run chunk credits: stranded chunks an EM ruled complete, with evidence.

Invariant: a credit lives in a run's record dir (beside ``notes.jsonl``). It is honoured by the
terminal commit of its run or of any later run of the same sizing, and a resume counts a credited
row landed. Stdlib only; spawns no process.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.credit_gate import BOOKKEEPING_CHUNK_IDS
from coordinator_core.ops.dispatch_emit.halted_run import _DATE_PREFIX, _STAMP
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


def _binds_sizing(run_dir: Path, sizing: str) -> bool:
    try:
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    rel = data.get("sizing_rel") if isinstance(data, dict) else None
    return isinstance(rel, str) and PurePosixPath(rel.replace("\\", "/")).as_posix() == sizing


def credits_for_sizing(repo_root: Path, sizing_rel: str) -> dict[str, str]:
    """``{chunk: evidence}`` across every run record dir binding ``sizing_rel``; never raises.

    Dirs merge oldest stamp first (``ask-`` and ``warp-`` together), so the newest credit wins. Only
    ``ask-<stamp>`` dirs stamped on or after the sizing's YYYY-MM-DD prefix and
    ``warp-<sizing stem>-<stamp>`` dirs are read; an undated name yields ``{}``.
    """
    sizing = PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()
    stem = PurePosixPath(sizing).stem
    dated = _DATE_PREFIX.match(stem)
    if dated is None:
        return {}
    floor = "".join(dated.groups())
    prefix = f"warp-{stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    candidates: list[tuple[str, Path]] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                name = entry.name
                if name.startswith("ask-"):
                    stamp = name[4:]
                    if not (_STAMP.fullmatch(stamp) and stamp[:8] >= floor):
                        continue
                elif name.startswith(prefix):
                    stamp = name[len(prefix):]
                    if not _STAMP.fullmatch(stamp):
                        continue
                else:
                    continue
                try:
                    if entry.is_dir():
                        candidates.append((stamp, Path(entry.path)))
                except OSError:
                    continue
    except OSError:
        return {}
    candidates.sort(key=lambda c: c[0])
    bound = [d for _, d in candidates if (d / CREDIT_FILE).is_file() and _binds_sizing(d, sizing)]
    return read(bound)
