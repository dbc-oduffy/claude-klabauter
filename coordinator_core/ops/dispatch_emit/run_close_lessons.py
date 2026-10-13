"""
coordinator_core.ops.dispatch_emit.run_close_lessons -- the run-close ``lessons`` leg.

Purpose: forwards the terminal judge's ``lesson_candidates`` to the central lessons outbox
as ``open`` candidate files via ``queue_promote.promote_lesson``.

Negative-spec:
  - No git, no subprocess; queue_promote is imported only when a candidate is written.
  - Title, body, change_kind and target_wiki pass verbatim; nothing is derived.
  - The written paths are never returned for ``also_commit``: the outbox is central state,
    not this repo.
  - Detail names outbox-relative file names only, never an absolute path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.ops.dispatch_emit.run_close import LegResult

AUTHORED_BY = "terminal-judge"
_FIELDS = ("title", "body", "change_kind", "target_wiki")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _entry_id(run_id: str, index: int) -> str:
    return f"run-close-{_SAFE.sub('-', run_id)}-{index}"


def _candidates(criterion: Any) -> list[Mapping[str, Any]]:
    if not isinstance(criterion, Mapping):
        return []
    raw = criterion.get("lesson_candidates")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, Mapping)]


def _existing(outbox: str, entry_ids: set[str]) -> set[str]:
    """Entry ids already present in *outbox* (matched on the file's ``id:`` line)."""
    found: set[str] = set()
    if not os.path.isdir(outbox):
        return found
    for name in os.listdir(outbox):
        if not name.endswith(".yaml"):
            continue
        try:
            with open(os.path.join(outbox, name), encoding="utf-8") as fh:
                head = fh.read(512)
        except OSError:
            continue
        match = re.search(r"^id: (\S+)\s*$", head, re.MULTILINE)
        if match and match.group(1) in entry_ids:
            found.add(match.group(1))
    return found


def _stamp(out_path: str, run_id: str) -> None:
    """Add ``status``, ``authored_by`` and ``run_id`` to the front matter of *out_path*."""
    text = Path(out_path).read_text(encoding="utf-8")
    extra = f"status: open\nauthored_by: {AUTHORED_BY}\nrun_id: {run_id}\n"
    head, sep, tail = text.rpartition("\n---\n")
    if not sep:
        raise ValueError("outbox file has no closing front-matter fence")
    Path(out_path).write_text(f"{head}\n{extra}---\n{tail}", encoding="utf-8", newline="\n")


def lessons_leg(root: Path, run_id: str, criterion: Optional[Mapping[str, Any]]) -> LegResult:
    """Write each judge ``lesson_candidates`` item as an open outbox candidate.

    ``not-applicable`` with no candidates; ``discharged`` when every candidate's entry id is
    already in the outbox; otherwise ``ran`` naming the files written.
    """
    cands = _candidates(criterion)
    if not cands:
        return LegResult("not-applicable", "judge returned no lesson candidates")
    for cand in cands:
        if not all(isinstance(cand.get(f), str) and cand.get(f) for f in _FIELDS):
            return LegResult("failed", "lesson candidate missing title, body, change_kind or target_wiki")

    from coordinator_core.ops import queue_promote

    try:
        outbox = queue_promote._outbox_root()
    except queue_promote._ContentRootUnresolvable as exc:
        return LegResult("failed", f"lessons outbox unresolvable: {type(exc).__name__}")

    ids = [_entry_id(run_id, i) for i in range(len(cands))]
    present = _existing(outbox, set(ids))
    if len(present) == len(ids):
        return LegResult("discharged", f"{len(ids)} candidate(s) already in lessons outbox")

    written: list[str] = []
    for entry_id, cand in zip(ids, cands):
        if entry_id in present:
            continue
        try:
            result = queue_promote.promote_lesson(
                cand["title"],
                cand["body"],
                cand["change_kind"],
                cand["target_wiki"],
                from_repo=f"{Path(root).name}-em",
                entry_id=entry_id,
            )
            _stamp(result["out_path"], run_id)
        except (ValueError, OSError, queue_promote._ContentRootUnresolvable) as exc:
            return LegResult("failed", f"lesson candidate not written: {type(exc).__name__}")
        written.append(os.path.basename(result["out_path"]))
    return LegResult("ran", "lessons-outbox/" + ", lessons-outbox/".join(written))
