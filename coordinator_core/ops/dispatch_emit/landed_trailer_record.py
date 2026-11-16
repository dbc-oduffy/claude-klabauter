"""coordinator_core.ops.dispatch_emit.landed_trailer_record -- a persisted answer to the
``git log -n 500 --grep=...`` that ``emit.resume_landed_state`` spawns, kept current by a bounded
in-process walk of only the commits since the recorded head.

A pure HEAD-keyed cache would miss on every call (peers move HEAD between gate calls); a full
in-process walk loses to one spawn (``git.commit_walk`` docstring). The shape here is the record
plus a pruned walk of ``HEAD ^recorded_head``, abandoned (``None``: spawn once and reseed) as soon
as it would read more than ``MAX_WALK_READS`` commit objects.

Storage: ``<git common dir>/coordinator/landed-trailers.json`` = ``{"head", "entries"}``; entries
are the 7-field tuples ``(subject, checkpoint, halt_plan, halted_by, chunks, sha, stop_rule)``,
newest first, capped at ``MAX_ENTRIES`` (the ``-n 500`` window).
"""

from __future__ import annotations

import heapq
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_sha

MAX_ENTRIES = 500
#: Commit objects the pruned walk may decode before the caller is told to spawn instead.
MAX_WALK_READS = 96
_SLOP = 5
_FIELD_SEP = "\x1f"
_VALUE_SEP = "\x1d"
_RECORD_FILE = ("coordinator", "landed-trailers.json")

_MATCH_RE = re.compile(r"^(?:Checkpoint-Plan: |Halt-Plan: |warp: record halted )", re.MULTILINE)
_TRAILER_LINE_RE = re.compile(r"^([A-Za-z0-9-]+)[ \t]*:[ \t]*(.*)$")
_RECOGNIZED_PREFIXES = ("Signed-off-by: ", "(cherry picked from commit ")
_A = 1  # reachable from HEAD
_U = 2  # reachable from the recorded head

_write_lock = threading.Lock()


class _Over(Exception):
    """The pruned walk exceeded its read budget or met an unreadable object."""


def record_path(repo_root) -> Path:
    """Path of the record file under the repo's git common dir."""
    return resolve_git_common_dir(repo_root).joinpath(*_RECORD_FILE)


def _split_record(record: str) -> Optional[tuple]:
    parts = record.strip("\r\n").split(_FIELD_SEP)
    if len(parts) < 6:
        return None
    parts = parts[:7]
    while len(parts) < 7:
        parts.append("")
    return tuple(parts)


def _parse_stdout(stdout: str) -> list:
    entries = []
    for record in stdout.split("\x1e"):
        entry = _split_record(record)
        if entry is not None:
            entries.append(entry)
    return entries[:MAX_ENTRIES]


def _trailer_block(message: str) -> list:
    """Trailer ``(key, value)`` pairs of ``message`` by git's rule: the last paragraph after the
    subject paragraph, accepted when it is all trailers, or holds a recognized prefix and at
    least a third as many trailer lines as other lines."""
    lines = message.rstrip("\n").split("\n")
    first_blank = next((i for i, line in enumerate(lines) if not line.strip()), None)
    if first_blank is None:
        return []
    end = len(lines)
    start = end
    while start > first_blank and lines[start - 1].strip():
        start -= 1
    block = lines[start:end]
    if start <= first_blank or not block:
        return []
    trailer_lines = other_lines = 0
    recognized = False
    pairs: list = []
    for line in block:
        if line[:1] in (" ", "\t") and pairs:
            pairs[-1][1] += "\n" + line
            trailer_lines += 1
            continue
        found = _TRAILER_LINE_RE.match(line)
        if found:
            trailer_lines += 1
            pairs.append([found.group(1), found.group(2).strip()])
            if line.startswith(_RECOGNIZED_PREFIXES):
                recognized = True
        else:
            other_lines += 1
            if line.startswith(_RECOGNIZED_PREFIXES):
                recognized = True
    if trailer_lines and not other_lines:
        return [(k, v) for k, v in pairs]
    if recognized and trailer_lines * 3 >= other_lines:
        return [(k, v) for k, v in pairs]
    return []


def _subject_of(message: str) -> str:
    lines = []
    for line in message.split("\n"):
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.strip())
    return " ".join(lines)


def _entry_for(sha: str, commit: dict) -> Optional[tuple]:
    message = commit.get("message") or ""
    if not _MATCH_RE.search(message):
        return None
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_TRAILER
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import STOP_RULE_TRAILER

    pairs = _trailer_block(message)

    def values(key: str) -> str:
        want = key.lower()
        return _VALUE_SEP.join(v.rstrip() for k, v in pairs if k.lower() == want)

    return (
        _subject_of(message),
        values("Checkpoint-Plan"),
        values(HALT_PLAN_TRAILER),
        values("Halted-By"),
        values("Lands-Chunks"),
        sha,
        values(STOP_RULE_TRAILER),
    )


def _delta(common_dir: Path, head: str, base: str) -> Optional[list]:
    """Commits reachable from ``head`` and not from ``base``, newest first, or ``None`` when
    ``base`` is not an ancestor of ``head`` or the walk would exceed ``MAX_WALK_READS``."""
    flags: dict = {}
    metas: dict = {}
    heap: list = []
    reads = 0

    def push(sha: str, flag: int) -> None:
        nonlocal reads
        known = flags.get(sha)
        if known is None:
            reads += 1
            if reads > MAX_WALK_READS:
                raise _Over
            meta = commit_meta(common_dir, sha)
            if meta is None:
                raise _Over
            flags[sha] = flag
            metas[sha] = meta
            heapq.heappush(heap, (-(meta["committer_epoch"] or 0), sha))
        elif flag & ~known:
            flags[sha] = known | flag
            if flag & _U:
                _propagate_uninteresting(sha)

    def _propagate_uninteresting(sha: str) -> None:
        stack = [sha]
        while stack:
            for parent in metas[stack.pop()]["parents"]:
                if parent in flags and not flags[parent] & _U:
                    flags[parent] |= _U
                    stack.append(parent)

    try:
        push(head, _A)
        push(base, _U)
        candidates: list = []
        slop = _SLOP
        while heap:
            if all(flags[s] & _U for _t, s in heap):
                slop -= 1
                if slop < 0:
                    break
            else:
                slop = _SLOP
            _neg, sha = heapq.heappop(heap)
            flag = flags[sha]
            if not flag & _U:
                candidates.append(sha)
            for parent in metas[sha]["parents"]:
                push(parent, flag)
    except _Over:
        return None
    if not flags[base] & _A:
        return None
    return [(sha, metas[sha]) for sha in candidates if not flags[sha] & _U]


def _load(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        head = data["head"]
        entries = [tuple(e) for e in data["entries"]]
        if not isinstance(head, str) or any(len(e) != 7 or not all(isinstance(x, str) for x in e) for e in entries):
            return None
        return {"head": head, "entries": entries}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _persist(common_dir: Path, path: Path, head: str, entries: list, reseed: bool) -> None:
    """Atomic replace that never regresses the record: skipped when the file already holds
    ``head`` or a descendant of it. A walk-based write (``reseed`` false) also needs the file's
    head to be an ancestor of ``head``; a reseed replaces an unrelated head (a rewrite).
    Single-process lock only: two processes can still interleave inside the window."""
    with _write_lock:
        current = _load(path)
        if current is not None:
            if current["head"] == head:
                return
            if reseed:
                if _delta(common_dir, current["head"], head) is not None:
                    return
            elif _delta(common_dir, head, current["head"]) is None:
                return
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"head": head, "entries": [list(e) for e in entries]}), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return


def trailer_entries(repo_root) -> Optional[list]:
    """The ``resume_landed_state`` log records as 7-tuples, newest first, capped at
    ``MAX_ENTRIES``; ``None`` when no usable record exists or the delta since its head is too
    large or not a descendant (caller spawns once and calls ``refresh_from_git_log``)."""
    common_dir = resolve_git_common_dir(repo_root)
    path = common_dir.joinpath(*_RECORD_FILE)
    record = _load(path)
    if record is None:
        return None
    head = head_sha(repo_root)
    if head is None:
        return None
    if record["head"] == head:
        return record["entries"]
    delta = _delta(common_dir, head, record["head"])
    if delta is None:
        return None
    fresh = [e for e in (_entry_for(sha, meta) for sha, meta in delta) if e is not None]
    entries = (fresh + record["entries"])[:MAX_ENTRIES]
    _persist(common_dir, path, head, entries, False)
    return entries


def refresh_from_git_log(repo_root, stdout: str, head: Optional[str] = None) -> list:
    """Seed or replace the record from one spawned ``git log`` answer and return its entries.
    Pass ``head`` read BEFORE the spawn; the default reads HEAD now, which mislabels the answer
    if HEAD moved in between."""
    entries = _parse_stdout(stdout)
    common_dir = resolve_git_common_dir(repo_root)
    head = head or head_sha(repo_root)
    if head is not None:
        _persist(common_dir, common_dir.joinpath(*_RECORD_FILE), head, entries, True)
    return entries
