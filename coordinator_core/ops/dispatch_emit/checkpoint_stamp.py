"""Checkpoint-commit parsing and the coded stamp for the rows a checkpoint landed.

``parse_checkpoint`` reads the message ``emit._checkpoint_commit_js`` composes;
``stamp_checkpoint`` flips the rows it names from ``open`` to ``coded`` at the
checkpoint's own sha, in-process (rows named on ``Checkpoint-Gate-Owed:`` or
``Checkpoint-Stop-Rule:`` stay ``open``), and lands one plan-only follow-up commit
carrying ``Checkpoint-Stamp: <sha>``. Stdlib-only at module top: callers gate the
import on a literal ``Checkpoint-Plan: `` test so a plain commit pays nothing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import NamedTuple, Optional

CHECKPOINT_SUBJECT = re.compile(r"^checkpoint\(wave [^)]*\): \d+ rows? — (?P<ids>.+)$")
CHECKPOINT_PLAN_LINE = re.compile(r"^Checkpoint-Plan: (?P<v>\S.*?)\s*$", re.M)
CHECKPOINT_GATE_OWED_LINE = re.compile(r"^Checkpoint-Gate-Owed:[ \t]*(?P<v>.+?)\s*$", re.M)
CHECKPOINT_STOP_RULE_LINE = re.compile(r"^Checkpoint-Stop-Rule:[ \t]*(?P<v>.+?)\s*$", re.M)
# Trap: a stopped row's bytes ARE in the commit that names it here; only its
# landed-ness is withheld, so a refire re-dispatches it.
STOP_RULE_TRAILER = "Checkpoint-Stop-Rule"
STAMP_TRAILER = "Checkpoint-Stamp"
_STAMP_LINE = re.compile(r"^" + STAMP_TRAILER + r":[ \t]*\S", re.M)


class Checkpoint(NamedTuple):
    plan_rel: str
    row_ids: frozenset
    gate_owed: frozenset
    stop_rule: frozenset = frozenset()  # carried, not landed


def _ids(text: str) -> frozenset:
    return frozenset(i.strip() for i in re.split(r"[,\s]+", text) if i.strip())


def parse_checkpoint(message: str) -> Optional[Checkpoint]:
    """The plan, named row ids, gate-owed and stop-rule ids of a checkpoint message; None when
    the subject is not a checkpoint's or the ``Checkpoint-Plan:`` line is absent."""
    subject = CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
    plan = CHECKPOINT_PLAN_LINE.search(message)
    if not subject or not plan:
        return None
    owed = CHECKPOINT_GATE_OWED_LINE.search(message)
    stopped = CHECKPOINT_STOP_RULE_LINE.search(message)
    return Checkpoint(
        plan.group("v").replace("\\", "/"),
        _ids(subject.group("ids")),
        _ids(owed.group("v")) if owed else frozenset(),
        _ids(stopped.group("v")) if stopped else frozenset(),
    )


def is_stamp_commit(message: str) -> bool:
    """True when the message carries a ``Checkpoint-Stamp:`` trailer."""
    return bool(_STAMP_LINE.search(message))


class CheckpointWalk(NamedTuple):
    landed: dict  # {checkpoint sha: frozenset(row ids)}, ids minus that checkpoint's gate-owed and stop-rule
    commits_read: int
    capped: bool  # the cap ended the walk with wanted ids still unfound


_PROCESS_HALF_SUFFIX = "(process files)"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_CREATED_LINE = re.compile(r"^created:[ \t]*[\"']?(\d{4})-(\d{2})-(\d{2})", re.M)
_DAY_SECONDS = 86400


def plan_created_floor(plan_text: str) -> Optional[int]:
    """Epoch below which no checkpoint of the plan can exist: its ``created:`` day at
    00:00 UTC minus one day of timezone slack; None when absent or unparseable."""
    head = plan_text.split("\n---", 1)[0] if plan_text.startswith("---") else ""
    m = _CREATED_LINE.search(head)
    if not m:
        return None
    try:
        from datetime import datetime, timezone

        day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(day.timestamp()) - _DAY_SECONDS


def checkpoint_rows_landed(
    worktree_root: Path,
    plan_rel: str,
    start_sha: str,
    wanted: frozenset,
    *,
    floor_epoch: Optional[int] = None,
    cap: int = 500,
) -> CheckpointWalk:
    """Which ``wanted`` rows did same-plan checkpoints land, at which sha, newest first.

    First-parent walk from ``start_sha``, in-process. Stamp commits, other plans'
    checkpoints and process halves are stepped over; a same-plan checkpoint records
    ``(row ids - gate-owed - stop-rule) & remaining`` at its own sha, so the newest landing wins.
    Ends when ``wanted`` is exhausted, below ``floor_epoch``, at ``cap`` commits, or at
    an unreadable or root commit. ``Checkpoint-Base:`` is not a stop.
    """
    empty = CheckpointWalk({}, 0, False)
    if not wanted or not plan_rel or not isinstance(start_sha, str) or not _FULL_SHA.fullmatch(start_sha):
        return empty
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common_dir = resolve_git_common_dir(Path(worktree_root))
    remaining = set(wanted)
    landed: dict = {}
    read = 0
    sha: Optional[str] = start_sha
    while remaining and sha is not None and read < cap:
        meta = commit_meta(common_dir, sha)
        if meta is None:
            break
        epoch = meta.get("committer_epoch")
        if floor_epoch is not None and epoch is not None and epoch < floor_epoch:
            break
        read += 1
        message = meta.get("message") or ""
        cp = None if is_stamp_commit(message) else parse_checkpoint(message)
        if cp is not None and cp.plan_rel == plan_rel:
            subject = (message.splitlines() or [""])[0].strip()
            ids = (cp.row_ids - cp.gate_owed - cp.stop_rule) & remaining
            if ids and not subject.endswith(_PROCESS_HALF_SUFFIX):
                landed[sha] = frozenset(ids)
                remaining -= ids
        parents = meta.get("parents") or []
        sha = parents[0] if parents else None
    return CheckpointWalk(landed, read, bool(remaining) and sha is not None and read >= cap)


def _plan_state(worktree_root: Path, rel: str, sha: str, data: bytes) -> str:
    """``clean`` (tracked at ``sha``, blob identical to ``data``), ``untracked`` (absent at
    ``sha``) or ``dirty`` (differs, or the tree cannot answer)."""
    from coordinator_core.git.commit_walk import commit_meta
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit

    common_dir = resolve_git_common_dir(worktree_root)
    blob = _blob_at_commit(common_dir, sha, rel) if commit_meta(common_dir, sha) else None
    if blob is None:
        return "dirty"
    if blob is ABSENT:
        return "untracked"
    if hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest() == blob:
        return "clean"
    from coordinator_core.git.content_hash import content_matches_index_sha

    return "clean" if content_matches_index_sha(worktree_root, rel, blob) is True else "dirty"


def stamp_checkpoint(
    worktree_root: Path, message: str, sha: str, *, session_id: Optional[str] = None
) -> dict:
    """Stamp the checkpoint's open rows ``coded`` at ``sha``, minus gate-owed and stop-rule rows.

    Returns ``{}`` when nothing flips; else ``{"rows_coded": {plan: [ids]}, "stamp_sha"}``
    (``stamp_sha`` None with ``stamp_skipped`` when the plan stays stamped on disk only: an
    untracked/ignored plan, or a tracked one whose bytes differ from ``sha`` beforehand);
    or ``{"stamp_error"}`` with every plan restored. Never raises. After a landed stamp commit the
    claims on its plan paths are released under ``session_id`` (skipped when it is falsy).
    """
    worktree_root = Path(worktree_root)
    originals: dict = {}
    try:
        cp = parse_checkpoint(message)
        if cp is None:
            return {}
        from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
        from coordinator_core.ops.dispatch_emit.terminal_commit import (
            _flip_rows_coded,
            _read_rel,
            _source_rows_by_plan,
        )
        from coordinator_core.session.claimed_write import replace_text

        ids = sorted(cp.row_ids - cp.gate_owed - cp.stop_rule)
        by_plan = _source_rows_by_plan(worktree_root, cp.plan_rel, ids)
        rows_coded: dict = {}
        state: dict = {}
        for plan_rel in sorted(by_plan):
            original = _read_rel(worktree_root, plan_rel)
            if original is None:
                continue
            updated, flipped = _flip_rows_coded(original, set(by_plan[plan_rel]), sha)
            if not flipped:
                continue
            invalid = check_plan_tasks_source(updated)
            if invalid is not None and check_plan_tasks_source(original) is None:
                raise ValueError(f"{plan_rel}: stamped spine fails schema: {invalid}")
            raw = (worktree_root / plan_rel).read_bytes()
            state[plan_rel] = _plan_state(worktree_root, plan_rel, sha, raw)
            replace_text(worktree_root / plan_rel, updated)
            originals[plan_rel] = original
            rows_coded[plan_rel] = flipped
        if not rows_coded:
            return {}

        commit_rels = [p for p in sorted(rows_coded) if state[p] == "clean"]
        out: dict = {"rows_coded": rows_coded, "stamp_sha": None}
        skipped = {state[p] for p in rows_coded} - {"clean"}
        if skipped:
            out["stamp_skipped"] = "plan_dirty" if "dirty" in skipped else "plan_untracked"
        if not commit_rels:
            return out

        from functools import partial

        from coordinator_core.git.commit import commit_paths, hash_worktree_blobs_via_spawn
        from coordinator_core.git.commit_trailers import apply_missing_trailers

        n = sum(len(rows_coded[p]) for p in commit_rels)
        body = f"mark {n} rows coded ({sha[:7]})\n\n{STAMP_TRAILER}: {sha}"
        body = apply_missing_trailers(body, worktree_root, commit_rels, session_id_override=session_id)
        outcome = commit_paths(
            worktree_root, commit_rels, body, prefer_deliberate_stage=True,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
        )
        out["stamp_sha"] = outcome.sha
        if outcome.sha:
            from coordinator_core.session.scope import release_committed_claims_or_retain

            release_committed_claims_or_retain(worktree_root, commit_rels, session_id, "checkpoint_stamp")
        return out
    except Exception as exc:  # noqa: BLE001 -- the checkpoint commit stands; the stamp is surfaced
        try:
            from coordinator_core.session.claimed_write import replace_text

            for rel, text in originals.items():
                replace_text(worktree_root / rel, text)
        except Exception:  # noqa: BLE001
            pass
        return {"stamp_error": repr(exc)}
