"""
coordinator_core.ops.ceremony.close_review -- JSON-RPC "close.review": pays the run-close
``review`` leg, the chain's unreceipted-ancestor range, and records its outcome in the run's
close-job record.

Purpose: the range walk (~211ms net, 2 procs) does not fit inside ``close.background``'s 500ms
bar beside its four other legs, so ``close.background``'s ``post-close`` stage launches this
module by ``detached_spawn.spawn_detached`` (``python close_review.py ...``). It reads the same
close-job record and writes the ``review`` leg through ``run_close.record_leg_outcome``; a leg it
never records reads ``failed`` at the record's deadline (``run_close.read_close_job``).

Negative-spec:
  - Runs only while the record still lists ``review`` as ``queued`` and its deadline has not passed.
  - A record without ``review_receipt``, ``run_base_sha`` or ``terminal_sha`` records ``failed``,
    never ``ran``.
  - The walk is capped at ``REVIEW_RANGE_CAP`` commits; a longer one is recorded ``failed``
    (truncated), never ``ran``.
  - A raising leg is recorded ``failed``; no leg detail carries an absolute or backslashed path.
  - Imports are lazy inside the leg; the op module itself stays cheap to load at server start.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.close_background import (
    JobContext,
    _context,
    _failed,
    _queued_legs,
    _scrub,
    _summary,
)
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

GENERATES = []

LEG = "review"

REVIEW_RANGE_CAP = 500
"""Commits the range walk examines; a longer walk is reported truncated, never ``ran``."""

_DELIVERABLE_RE = re.compile(r"^deliverable_id:\s*[\"']?([A-Za-z0-9._-]+)", re.MULTILINE)
_TERMINAL_SHA_RE = re.compile(r"^\s+terminal_commit_sha:\s*[\"']?([0-9a-f]{40})\b", re.MULTILINE)
_HEAD_CHARS = 1500


def _head(path: Path, chars: int = _HEAD_CHARS) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(chars)
    except OSError:
        return ""


def _load_record(ctx: JobContext) -> dict[str, Any]:
    try:
        data = json.loads((ctx.root / run_close.close_job_path(ctx.run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _deliverable_ids(ctx: JobContext) -> frozenset[str]:
    found = (_DELIVERABLE_RE.search(_head(ctx.root / Path(baton).as_posix())) for baton in ctx.claimed_batons)
    return frozenset(m.group(1) for m in found if m)


def _plan_tips(root: Path, ids: frozenset[str], own_receipt: str) -> list[str]:
    """``review_stamp.terminal_commit_sha`` of tracked plans sharing a claimed deliverable."""
    tips: list[str] = []
    plans = root / "docs" / "plans"
    if not plans.is_dir():
        return tips
    for path in sorted(plans.glob("*.md")):
        if path.relative_to(root).as_posix() == own_receipt:
            continue
        head = _head(path)
        match = _DELIVERABLE_RE.search(head)
        if not match or match.group(1) not in ids:
            continue
        stamp = _TERMINAL_SHA_RE.search(_head(path, 12000))
        if stamp:
            tips.append(stamp.group(1))
    return tips


def _record_tips(root: Path, run_id: str, batons: frozenset[str]) -> list[str]:
    """``terminal_sha`` of this clone's other close-job records whose review leg ran."""
    tips: list[str] = []
    jobs = root / run_close.close_job_path(run_id).rsplit("/", 1)[0]
    if not jobs.is_dir():
        return tips
    for path in sorted(jobs.glob("*.json")):
        if path.stem == run_id:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not batons.intersection(data.get("claimed_batons") or ()):
            continue
        reviewed = any(
            isinstance(bucket, dict) and isinstance(bucket.get("review"), dict)
            and bucket["review"].get("status") == "ran"
            for bucket in (data.get("legs"), data.get("settled"))
        )
        sha = data.get("terminal_sha")
        if reviewed and isinstance(sha, str):
            tips.append(sha)
    return tips


def _review(ctx: JobContext) -> LegResult:
    """Name the chain's code commits since the last receipted run that no review credit covers."""
    record = _load_record(ctx)
    receipt = record.get("review_receipt")
    if not isinstance(receipt, str) or not receipt:
        return LegResult("failed", "close-job record names no review_receipt")
    for key in ("run_base_sha", "terminal_sha"):
        if not isinstance(record.get(key), str) or not record[key]:
            return LegResult("failed", f"close-job record has no {key}; review range not computed")
    ids = _deliverable_ids(ctx)
    if not ids:
        return LegResult("ran", _scrub(f"receipt {receipt}; claimed batons carry no deliverable chain"))

    from coordinator_core.review_trail.unreceipted_range import unreceipted_ancestors

    batons = frozenset(Path(b).as_posix() for b in ctx.claimed_batons)
    own = record["terminal_sha"]
    tips = list(dict.fromkeys(
        t for t in (*_plan_tips(ctx.root, ids, receipt), *_record_tips(ctx.root, ctx.run_id, batons)) if t != own
    ))
    result = unreceipted_ancestors(
        str(ctx.root),
        record["run_base_sha"],
        exclude_tips=tips,
        member=lambda _sha, _session, deliverable: deliverable in ids,
        cap=REVIEW_RANGE_CAP,
    )
    if result.error:
        return LegResult("failed", _scrub(f"review range not computed: {result.error}"))
    if result.truncated or result.uncovered:
        return LegResult(
            "failed",
            run_close.render_unreceipted(result.uncovered, truncated=result.truncated, cap=REVIEW_RANGE_CAP),
        )
    since = f"since {tips[0][:12]}" if tips else "no receipted ancestor"
    return LegResult("ran", _scrub(f"receipt {receipt}; 0 unreceipted ancestors; {since}"))


def run_review(ctx: JobContext) -> dict[str, LegResult]:
    """Pay the ``review`` leg when the record still lists it queued and records its outcome.

    Raises FileNotFoundError when the run has no close-job record. Returns leg -> outcome (empty
    when the leg is not queued).
    """
    if LEG not in _queued_legs(ctx.root, ctx.run_id, (LEG,)):
        return {}
    try:
        result = _review(ctx)
    except Exception as exc:  # a raising leg is recorded, never propagated
        result = _failed("", exc)
    run_close.record_leg_outcome(ctx.root, ctx.run_id, LEG, result.status, result.detail)
    return {LEG: result}


@register_op("close.review")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "close.review": pay the run's queued ``review`` leg.

    Params: run_id (str), claimed_batons (list of repo-relative paths).
    repo_root: the git common dir injected by dispatch. Returns ``{run_id, legs}`` telemetry;
    the caller never acts on it (FIRE_AND_FORGET). A malformed request or a missing close-job
    record returns ``{"error": ...}``.
    """
    from coordinator_core.lifecycle import main_worktree_root

    if repo_root is None:
        return {"error": "close.review requires a repo root"}
    try:
        ctx = _context(main_worktree_root(Path(repo_root)), Path(repo_root), params or {})
        return _summary(ctx.run_id, run_review(ctx))
    except (ValueError, FileNotFoundError) as exc:
        return {"error": _scrub(f"{type(exc).__name__}: {exc}")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry for ``spawn_detached``: ``close_review.py <run_id> --root DIR --params JSON``."""
    parser = argparse.ArgumentParser(description="close.review job")
    parser.add_argument("run_id")
    parser.add_argument("--root", required=True)
    parser.add_argument("--params", default="{}", help="JSON: claimed_batons")
    args = parser.parse_args(argv)
    from coordinator_core.git.git_dir import resolve_git_common_dir

    root = Path(args.root)
    try:
        params = {**json.loads(args.params), "run_id": args.run_id}
        run_review(_context(root, resolve_git_common_dir(root), params))
    except (ValueError, FileNotFoundError) as exc:
        print(_scrub(f"close.review: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
