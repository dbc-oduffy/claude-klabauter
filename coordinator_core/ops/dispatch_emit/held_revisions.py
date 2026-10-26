"""Sizing-keyed store of held `workflow.revise` revisions for runs not yet emitted.

One append-only JSONL file per sizing (`<RECORD_DIR_ROOT>/held-revisions/<key>.jsonl`), so whichever
run id an emit mints finds its revisions with a single read at a computed path. Entries are held
rows; a `{consumed, by, at}` marker line closes one. Nothing is deleted.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.session.declared_writes import declare_write

STORE_DIR = "held-revisions"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_path(root: Path | str, sizing_rel: str) -> Path:
    """The store file for `sizing_rel`: the key is the sha256 of its POSIX form, so no glob is needed."""
    key = hashlib.sha256(PurePosixPath(str(sizing_rel).replace("\\", "/")).as_posix().encode("utf-8")).hexdigest()[:16]
    return Path(root) / RECORD_DIR_ROOT / STORE_DIR / f"{key}.jsonl"


def _append(path: Path, entry: dict) -> None:
    from coordinator_core.atomic_append import append_line

    path.parent.mkdir(parents=True, exist_ok=True)
    append_line(path, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
    declare_write(path)


def _read(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn tail line from a crashed writer; the rest stands
        if isinstance(entry, dict):
            out.append(entry)
    return out


def hold(root, sizing_rel: str, run_id: str, *, text: str | None = None, note: str | None = None,
         sender: str | None = None) -> dict:
    """Append one held revision (a `text` verbatim or a `note`) for `sizing_rel`; returns the entry."""
    if not (text or note):
        raise ValueError("hold needs a text or a note")
    entry: dict = {"id": "H-" + uuid.uuid4().hex[:10], "run_id": run_id, "sizing_rel": str(sizing_rel)}
    if text:
        entry["text"] = text
    if note:
        entry["note"] = note
    entry["at"] = _now()
    if sender:
        entry["from"] = sender
    _append(store_path(root, sizing_rel), entry)
    return entry


def pending(root, sizing_rel: str) -> list[dict]:
    """Open entries (no consumed marker) for `sizing_rel`, oldest first, from one read."""
    rows = _read(store_path(root, sizing_rel))
    closed = {r["consumed"] for r in rows if "consumed" in r}
    return [r for r in rows if "consumed" not in r and r.get("id") and r["id"] not in closed]


def mark_consumed(root, sizing_rel: str, ids, consumer_run_id: str) -> None:
    """Close `ids` with a consumed marker each. Call only after the fold is durable."""
    path = store_path(root, sizing_rel)
    for hid in ids:
        _append(path, {"consumed": hid, "by": consumer_run_id, "at": _now()})


def drain(root, sizing_rel: str, consumer_run_id: str) -> list[dict]:
    """`pending` then `mark_consumed`: for callers with no fold that can fail between the two."""
    entries = pending(root, sizing_rel)
    mark_consumed(root, sizing_rel, [e["id"] for e in entries], consumer_run_id)
    return entries


def unconsumed(root, sizing_rel: str, run_id: str | None = None) -> list[dict]:
    """Open entries for `sizing_rel`. The store is sizing-keyed, so `run_id` (the asking run) does not
    narrow the result: a revision held under another run id still belongs to this sizing's emit."""
    return pending(root, sizing_rel)
