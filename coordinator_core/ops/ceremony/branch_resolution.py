"""
coordinator_core.ops.ceremony.branch_resolution — branch-resolution engine.

Purpose: Reads disk and resolves all branch signals from the Branch Inventory in
docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.node-map.md into a
PipelineContext.  This is the pure read-mostly ENGINE — session-shape reading,
the 17-branch resolution, commit-log/idempotency/memo-scan primitives — factored
out so it can be shared by more than one op.

History: this module WAS ``wsc_resolve.py``, phase 1 of the two-phase
``ceremony.wsc_resolve`` / ``ceremony.wsc_commit`` pipeline. That pipeline was
superseded by the single-pass ``ceremony.wsc_tail`` op (2026-07-16 pure-Python
tail rebuild); ``ceremony.wsc_resolve``'s own JSON-RPC op registration was
retired 2026-07-29 (kill-list op removal, alongside ``wsc_commit.py``, deleted
outright) — no live caller ever dispatched it as an op.

GRAVESTONE (2026-08-30): the retired handler's undecorated BODY —
``resolve_session_branches``, its ``_resolve_branches`` engine (1,273 lines),
and the helpers reachable ONLY from that chain (``_grep_disposition``,
``_find_consumed_handoff``, ``_list_candidate_plans``,
``_dedup_handoffs_by_relpath``, ``_sanitize_detector_b_hits``,
``_parse_frontmatter``, ``_resolve_in_reply_to_target``, ``_scan_open_memos``,
``_read_completeness_mirror``, ``_check_doc_fragile_domain``,
``_check_plan_claim``, ``_check_idempotency``, plus the
``_CHAIN_TERMINAL_ONLY_STEPS`` / ``_DOC_FRAGILE_EXTENSIONS`` constants and the
node_handlers/pipeline_context/wsc_disposition/receipt_emit/receipt_schema/
completion_nature imports those helpers alone required) — were deleted
outright. The whole-tree AST scan run before deletion found no non-test
caller for any of them; ``tests/test_branch_resolution.py``'s own coverage of
that chain (and the four symbols themselves) was removed alongside it. See
``state/kill-ledger.md`` for the per-symbol disposition.

This module survives under its current name because live callers
(``coordinator_core.session.session_facts``,
``coordinator_core.quick_wrap_assemble``) import ``analyze_session_scoping`` /
``ScopingVerdict`` / ``SCOPING_METHOD_*`` / the ``_BRIGHTLINE_*`` thresholds /
``_session_touched_paths`` / ``session_commit_count_attributed`` directly —
the scoping-analysis + session-commit-primitive engine is live; the retired
op handler's body was not.

Branch resolution reads session-shape.json PYTHON-NATIVELY:
  Path: <common_dir>/coordinator-sessions/<sid>/session-shape.json
  Schema: {schema_version:1, pickup:{...}, actioned_memos:[...],
           plan:{scope_mode:...}, magnitude:...}

L1b absent-session-shape fallback (REQUIRED — see plan § C2.2):
  When session-shape.json is absent OR the "pickup" field is absent from the file,
  fall back to: git grep -rl "consumed_by: <sid>" state/handoffs/
  Sessions started before L1's producer was deployed have no session-shape.json;
  treating absence as pickup.happened=false produces a chain-terminal mis-disposition.
  The grep fallback MUST be in place — it is not optional.

Callers resolve worktree_root/common_dir themselves — this module has no op
scope of its own (it registers no op; ``main_worktree_root(common_dir)`` is the
caller's job, same as it was for the retired op handler).

Spec backlink:
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § Design + C2.2
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.node-map.md § Branch Inventory
"""

from __future__ import annotations
import sys

import json
import logging
import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from functools import lru_cache

from coordinator_core import session_attribution
from coordinator_core.coverage import _get_handoff_consumed_by
from coordinator_core.ipc import CEREMONY_BUDGET_SECS, get_op_handler
from coordinator_core.op_budget_suspension import OpSuspendedError
from coordinator_core.ops.session_commits import (
    resolve_session_commits as _resolve_session_commits_primitive,
)
from coordinator_core.ops.ceremony.resolver import (
    resolve_in_repo as _resolve_in_repo,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Partition-mandatory thresholds (mirrors review-brightline-gate.sh defaults;
# Port of: review-brightline-gate.sh, DoE b5a4192c, 2026-07-20)
_BRIGHTLINE_LOC = 500
_BRIGHTLINE_COMMITS = 5
_BRIGHTLINE_SURFACES = 4

# ---------------------------------------------------------------------------
# Session-shape.json reader with L1b fallback
# ---------------------------------------------------------------------------


def _read_session_shape(common_dir: Path, sid: str) -> tuple[dict[str, Any], str]:
    """Read session-shape.json for ``sid`` from the coordinator-sessions dir.

    Returns (shape_dict, source) where source is one of:
      "session_shape"  — read from session-shape.json
      "absent"         — file absent; L1b grep fallback should be used

    Python-native read — do NOT shell out to bin/coordinator-session-shape.
    Per plan § C2.2: read from .git/coordinator-sessions/<sid>/session-shape.json
    directly.
    """
    shape_path = common_dir / "coordinator-sessions" / sid / "session-shape.json"
    if not shape_path.exists():
        return {}, "absent"

    try:
        with open(shape_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data, "session_shape"
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("wsc_resolve: could not read session-shape.json for %s: %s", sid, exc)
        return {}, "absent"


# ---------------------------------------------------------------------------
# started_at reader (C1 — STEP_2_6_3 chain-slug case-a)
# ---------------------------------------------------------------------------


def _read_started_at(common_dir: Path, sid: str) -> str | None:
    """Read started_at for ``sid`` from the coordinator-sessions dir.

    Returns the trimmed ISO-8601 string from
    ``<common_dir>/coordinator-sessions/<sid>/started_at``, or ``None`` when
    the file is absent or empty.

    Mirrors _read_session_shape (same dir, sibling file).  Python-native read.

    Spec backlink:
      docs/plans/2026-07-06-wsc-resolve-consume-doe-signals-x-to-d.md § C1
    """
    started_at_path = common_dir / "coordinator-sessions" / sid / "started_at"
    if not started_at_path.exists():
        return None
    try:
        value = started_at_path.read_text(encoding="utf-8").strip()
        return value if value else None
    except OSError as exc:
        log.warning("wsc_resolve: could not read started_at for %s: %s", sid, exc)
        return None


# ---------------------------------------------------------------------------
# Core git helper — defined here (before _scan_session_scratch) so callers-before-
# helpers ordering is restored.  Session-specific git helpers follow after the scan.
# Review: code-reviewer F7 — moved _git_run above _scan_session_scratch.
# ---------------------------------------------------------------------------


#: Synthetic returncode `_git_run` uses for a TimeoutExpired specifically —
#: distinct from the generic `returncode=2` an OSError (not-a-repo, git not
#: on PATH, etc.) still gets. 124 mirrors the POSIX `timeout(1)` convention.
#: Review: code-reviewer F1 — the two failure classes were indistinguishable
#: by returncode alone, so a caller wanting to tell "git timed out" from "git
#: failed some other way" had no signal to read. See _trailer_reliable /
#: _started_at_candidate_range / _range_commit_count / _range_diff_loc /
#: _range_touched_paths, the five callers this exists for.
_GIT_TIMEOUT_RETURNCODE = 124


def _git_run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git subcommand, suppressing exceptions; return the CompletedProcess.

    A timeout gets its own returncode (`_GIT_TIMEOUT_RETURNCODE`), distinct
    from the generic `returncode=2` any other OSError still produces — see
    that constant's docstring for why the split exists.
    """
    try:
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # A subprocess bound wider than the op's own end-to-end budget is
            # unreachable dead configuration -- the dispatch layer abandons the
            # await before it could ever fire, and the process keeps occupying
            # the box after the caller is gone. Deriving from the ceremony
            # budget means it tightens automatically as the ratchet lowers.
            timeout=CEREMONY_BUDGET_SECS,
            **no_console_creationflags(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("wsc_resolve: git %s timed out: %s", args[0] if args else "?", exc)
        proc = subprocess.CompletedProcess(args=["git"] + args, returncode=_GIT_TIMEOUT_RETURNCODE)
        proc.stdout = ""
        proc.stderr = str(exc)
        return proc
    except OSError as exc:
        # Return a synthetic failed result so callers can treat gracefully.
        log.warning("wsc_resolve: git %s failed: %s", args[0] if args else "?", exc)
        proc = subprocess.CompletedProcess(args=["git"] + args, returncode=2)
        proc.stdout = ""
        proc.stderr = str(exc)
        return proc


# ---------------------------------------------------------------------------
# Filesystem-mtime scratch scan (C1 — STEP_2_67A X→D conversion)
# ---------------------------------------------------------------------------

#: Keep-listed basenames that are feature-scoped artifacts, not transient scratch.
#: Canonical source: docs/plans/2026-06-15-workstream-complete-self-clean.md L76-81.
_SCRATCH_KEEP_LIST_NAMES: frozenset[str] = frozenset({
    "todo.md",
    "plan.md",
    "completion-log.md",
})


def _scan_session_scratch(
    worktree_root: Path,
    started_at: str | None,
) -> int | None:
    # Review: code-reviewer F2 — sid parameter removed; function scans tasks/
    # unconditionally (all untracked post-started_at files, regardless of session
    # authorship) — sid presence contradicted this intent and was never used.
    """Count untracked scratch files under tasks/ authored after the session started_at.

    Purpose: deterministic enumeration predicate for STEP_2_67A — answers "are there
    session-authored transient files?" without per-file keep/delete judgment (that is
    STEP_2_67B, a J-node).

    Returns:
      None  — started_at absent OR scan/subprocess error (graceful-negative; caller
              emits X-shaped no-signal rather than a false-zero D).
      int   — count of untracked, non-keep-listed files under tasks/ with
              st_mtime > started_epoch (strict-after, matching canonical).

    Parse discipline (CRITICAL — fleet portability):
      started_at.replace('Z', '+00:00') → datetime.fromisoformat() yields an AWARE
      UTC datetime.  Naive rstrip('Z') yields a naive datetime whose .timestamp()
      is anchored to LOCAL time, skewing the epoch by ±offset (up to ±8 h on
      US-Pacific fleet nodes) → systematic false-negative D.  This is the F2 defect
      this spinoff exists to fix.

    Exclusions applied (deterministic, rule-based — stays D per D/J boundary):
      1. Keep-list basenames: todo.md, plan.md, completion-log.md (feature-scoped artifacts).
      2. *.plan.md endswith-match; .completion substring-match (all positions).
         Review: code-reviewer F3 — code uses 'in name' (any position), not suffix-only.
      3. Git-tracked files (only UNTRACKED paths count as transient scratch).

    Namespace: tasks/ only (concrete, machine-enumerable session-scratch namespace).
    tasks/ absent → count=0 (not None; started_at is present so the signal is legible).

    Spec backlink:
      docs/plans/2026-07-06-wsc-resolve-2-67a-x-to-d-mtime-scan.md § C1
    """
    if started_at is None:
        return None

    # Parse epoch tz-aware — .replace yields an AWARE UTC datetime; .timestamp() is correct.
    try:
        started_epoch = datetime.fromisoformat(
            started_at.replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, AttributeError) as exc:
        log.warning(
            "wsc_resolve: _scan_session_scratch: could not parse started_at %r: %s",
            started_at, exc,
        )
        return None

    tasks_dir = worktree_root / "tasks"
    if not tasks_dir.exists():
        return 0

    # Compute git-tracked set once — membership test is O(1) per file.
    tracked: set[str] = set()
    try:
        result = _git_run(["ls-files", "tasks/"], cwd=worktree_root)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped:
                    tracked.add(stripped)
        # Non-zero returncode (e.g. not a git repo) → treat tracked as empty;
        # all files will be counted as untracked (conservative over-count, safe).
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "wsc_resolve: _scan_session_scratch: git ls-files failed: %s", exc
        )
        return None

    # Review: code-reviewer Finding 2 — .rglob("*") silently swallows
    # PermissionError while walking subdirectories (verified: a chmod-0o000
    # dir under it yields nothing, no exception raised), the same dead-guard
    # shape fixed elsewhere in this module (_scan_open_memos → iterdir(),
    # _check_idempotency/find_all_consumed_handoffs → os.walk(onerror=...)).
    # Converted to os.walk(onerror=...) so a permission-denied subtree forces
    # the existing None graceful-negative contract (an X-node upstream)
    # rather than silently undercounting scratch_count with no signal.
    walk_errors: list[OSError] = []
    count = 0
    for dirpath, _dirnames, filenames in os.walk(tasks_dir, onerror=walk_errors.append):
        for fn in filenames:
            entry = Path(dirpath) / fn
            name = entry.name

            # Keep-list: exact basename exclusion.
            if name in _SCRATCH_KEEP_LIST_NAMES:
                continue
            # Keep-list: suffix-pattern exclusion (*.plan.md and *.completion*).
            if name.endswith(".plan.md") or ".completion" in name:
                continue

            # Git-tracked exclusion: only untracked files are transient scratch.
            try:
                rel = entry.relative_to(worktree_root).as_posix()
            except ValueError:
                print(f"skip: _scan_session_scratch: rel = entry.relative_to(worktree_root).as_posix() failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            if rel in tracked:
                continue

            # Mtime filter: strict-after (> not >=), matching canonical self-clean.
            try:
                mtime = entry.stat().st_mtime
            except OSError as exc:
                log.warning(
                    "wsc_resolve: _scan_session_scratch: stat failed for %s: %s",
                    entry, exc,
                )
                continue
            if mtime > started_epoch:
                count += 1

    if walk_errors:
        for exc in walk_errors:
            log.warning(
                "wsc_resolve: _scan_session_scratch: cannot scan %s — %s; "
                "subtree dropped (would silently undercount scratch_count) — "
                "forcing graceful-negative",
                getattr(exc, "filename", tasks_dir), exc,
            )
        return None

    return count



# ---------------------------------------------------------------------------
# Git helpers (read-only subprocess calls — session-specific; _git_run defined above)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=128)
def _cached_session_commits(
    worktree_root_str: str, sid: str
) -> Optional[tuple]:
    """Memoized wrapper over C4's ``session.commits`` primitive
    (``ops.session_commits :: resolve_session_commits``) — the ONE
    ``git log --numstat`` invocation ``_session_commit_log``,
    ``_session_touched_paths``, ``session_commit_count_attributed`` and
    ``_session_diff_loc`` now share, in place of four separate full-history
    ``--grep`` walks over the identical commit set (Finding 1). Cached per
    ``(worktree_root, sid)`` for the lifetime of this cold-spawn process only
    — every invocation of this engine is a cold spawn (repo's own
    ``machine-load-norm`` doctrine), so there is no cross-invocation
    staleness to guard against.

    Returns ``None`` on any primitive failure (never raises) — distinct from
    ``()`` (a genuine zero-commit session), so callers preserve C4's own
    computed/degraded distinction rather than collapsing a git failure into
    a false zero. Callers below own translating ``None`` into their own
    existing degraded/graceful-empty contract.

    Anchoring: this primitive resolves the anchored/unanchored split C4's
    module docstring documents (``^Session-Id: <sid>``, unanchored at the
    end) — same form ``_session_commit_log`` / ``_session_touched_paths`` /
    ``session_commit_count_attributed`` / ``_session_diff_loc`` already used
    before C5, so migrating onto it changes zero observable matches for
    those four sites.

    Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C5.
    """
    try:
        return tuple(_resolve_session_commits_primitive(Path(worktree_root_str), sid))
    except (ValueError, RuntimeError) as exc:
        log.warning(
            "branch_resolution: session.commits primitive failed for sid=%s: %s",
            sid, exc,
        )
        return None


def _session_commit_log(worktree_root: Path, sid: str) -> list[str]:
    """Return one-line commit messages for commits tagged with Session-Id: sid,
    most-recent-first (matches this function's pre-C5 ``git log`` default
    order; C4's primitive returns oldest-first via ``--reverse``, so the
    result is reversed here).

    C5: derived from C4's ``session.commits`` primitive — see
    ``_cached_session_commits``.
    """
    commits = _cached_session_commits(str(worktree_root), sid) or ()
    return [c["subject"] for c in reversed(commits)]


def _session_touched_paths(worktree_root: Path, sid: str) -> list[str]:
    """Return file paths touched by commits tagged with Session-Id: sid,
    most-recent-commit-first (matches this function's pre-C5 ``git log
    --name-only`` default order); per-commit path order preserved.

    C5: derived from C4's ``session.commits`` primitive — see
    ``_cached_session_commits``.
    """
    commits = _cached_session_commits(str(worktree_root), sid) or ()
    paths: list[str] = []
    for c in reversed(commits):
        paths.extend(c["touched_paths"])
    return paths


def _session_added_plans(
    worktree_root: Path,
    sid: str,
    started_at: str,
) -> list[str]:
    """Return paths for plan docs ADDED by commits tagged with Session-Id: sid since started_at.

    Uses git log --diff-filter=A (ADDED only — chain-opening plan authored this
    session) combined with --since=<started_at> and --grep=Session-Id: <sid>
    over docs/plans/*.md.  Deduped; graceful-empty on any non-zero _git_run return.

    Predicate pin: --diff-filter=A is intentional — case-a is the chain-opening
    plan ADDED this session (not merely touched).  Compose with --since=<started_at>
    so session-attributed and temporally-bounded adds are the signal.

    git pathspec note: ``docs/plans/*.md`` — git * crosses /; harmless (flat dir).

    C5 continuation (docs/plans/2026-08-18-a-session-always-has-a-baton.md
    § C5): migrated onto C4's ``session.commits`` primitive now that it
    exposes a per-file ``status`` alongside its numstat counts (``--raw``
    composed alongside ``--numstat`` in the SAME invocation — see that
    primitive's module docstring) — the added-status equivalent of
    ``--diff-filter=A`` this site previously needed a second git call for —
    plus a per-commit ``committer_epoch`` (``%ct``, same header line as
    ``sha``/``subject``), the ``--since``-equivalent this function needs for
    its temporal floor. Both are read off ONE ``_cached_session_commits``
    call — no second git invocation. Filters in Python: ``status == "A"``
    plus a ``docs/plans/`` prefix + ``.md`` suffix match (mirrors the
    ``docs/plans/*.md`` pathspec — a flat dir, so no ``/`` crossing to guard
    against) plus ``committer_epoch >= started_epoch`` (mirrors ``--since``,
    which git evaluates as an inclusive floor on commit date).

    Spec backlink:
      docs/plans/2026-07-06-wsc-resolve-consume-doe-signals-x-to-d.md § C1
      docs/plans/2026-08-18-a-session-always-has-a-baton.md § C5
    """
    try:
        started_epoch = datetime.fromisoformat(
            (started_at or "").replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, AttributeError):
        return []

    commits = _cached_session_commits(str(worktree_root), sid)
    if commits is None:
        return []

    seen: set[str] = set()
    paths: list[str] = []
    for commit in commits:
        if commit.get("committer_epoch", 0) < started_epoch:
            continue
        for f in commit["files"]:
            if f.get("status") != "A":
                continue
            path = f["path"]
            if not (path.startswith("docs/plans/") and path.endswith(".md")):
                continue
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def session_commit_count_attributed(worktree_root: Path, sid: str) -> dict:
    """Attributed session commit count: `git log --grep=Session-Id: <sid>`.

    Returns C1(c)'s degraded-with-evidence shape (docs/decisions/DR-319 —
    `_compute_dirty_tree_attribution`'s reference shape), not a bare int. A failed git
    call and a genuinely-zero-commit session are NOT the same value, and this producer
    is the seam where that distinction must first exist — collapsing a git failure into
    `0` here is what left AC3 unsatisfiable at the facade one layer up
    (`coordinator_core/session/session_facts.py`), which cannot recover a distinction
    its producer already destroyed.

    Promoted from the former private `_session_commit_count` (plan
    docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md, C2 sub-task,
    the Staff Engineer F4): the underscore name was being reached across packages by
    `quick_wrap_assemble`, the exact accretion `assemblers.md` § 7 warns against for
    `pickup_assemble`. This is now the one importable producer both packages call.

    Shapes:
      - `{"degraded": False, "value": <int commit count>}`
      - `{"degraded": True, "evidence": "<why the probe could not run>"}`

    KNOWN UNDERCOUNT (the Staff Engineer F5, R-11/DR-319): `--grep=Session-Id:` only sees commits
    carrying the trailer. A commit made via plumbing (`git commit-tree`, bypassing the
    porcelain `commit` path) loses the trailer, so `value: 0` is ALSO reachable for a
    session that committed real work. This producer has no signal to distinguish "no
    commits" from "commits, untagged" — a caller serving this fact declares that limit
    as part of the served fact rather than silently inheriting it.

    `_session_diff_loc` (below) shares C4's primitive too (C5) — both are now
    derived from the SAME cached ``session.commits`` call as
    ``_session_commit_log`` / ``_session_touched_paths``, so the four sites
    collapse onto one ``git log --numstat`` invocation, not four separate
    walks.

    C5: derived from C4's ``session.commits`` primitive — see
    ``_cached_session_commits``. The computed/degraded distinction is
    preserved: a primitive failure (``_cached_session_commits`` returning
    ``None``) reports ``degraded: True`` here, never collapsed into
    ``value: 0``.
    """
    commits = _cached_session_commits(str(worktree_root), sid)
    if commits is None:
        return {
            "degraded": True,
            "evidence": (
                f"session.commits primitive failed for sid={sid!r} "
                "(see log for underlying git error)"
            ),
        }
    return {"degraded": False, "value": len(commits)}


def _session_diff_loc(worktree_root: Path, sid: str) -> int:
    """Approximate total lines-of-change for commits tagged with Session-Id: sid.

    C5: derived from C4's ``session.commits`` primitive's per-commit
    ``added``/``deleted`` numstat totals (see ``_cached_session_commits``),
    in place of its own ``git log --stat`` walk. Binary rows are excluded
    from both the old ``--stat``-summary form and the new ``--numstat``-
    derived form (git's own ``--stat`` summary line never counts binary
    changes either), so this migration changes no observable total. Returns
    0 on any primitive failure (graceful-absent, unchanged contract).
    """
    commits = _cached_session_commits(str(worktree_root), sid) or ()
    return sum(c["added"] + c["deleted"] for c in commits)


def _session_surface_count(paths: list[str]) -> int:
    """Count the number of distinct top-level directory surfaces touched.

    Surfaces = distinct first components of repo-relative paths (e.g. "docs",
    "coordinator_core", "state").  Used for the brightline ≥4-surfaces threshold.
    """
    surfaces = set()
    for p in paths:
        parts = Path(p).parts
        if parts:
            surfaces.add(parts[0])
    return len(surfaces)


def _record_git_timeout(result: subprocess.CompletedProcess, caller: str, warnings: list[str] | None) -> None:
    """Append a degrade-loud entry to `warnings` iff `result` is a `_git_run` timeout.

    Mirrors this module's `scan_errors` out-param idiom (see
    `_find_all_consumed_handoffs`) rather than resolver.py's tuple-return
    shape — these five call sites already return a bare scalar the ~4,450-
    line test suite asserts on directly (`is True`, `== ""`, …), and an
    optional out-param leaves that contract untouched. A `None` `warnings`
    (the default) reproduces the pre-existing silent-degrade behaviour
    exactly, for any caller that hasn't opted in yet.
    """
    if warnings is not None and result.returncode == _GIT_TIMEOUT_RETURNCODE:
        warnings.append(f"{caller}: {result.stderr}")


def _range_commit_count(
    worktree_root: Path, candidate_range: str, *, warnings: list[str] | None = None
) -> int | None:
    """Return the number of commits within candidate_range.

    Range-scoped counterpart to ``session_commit_count_attributed`` for the
    SCOPING_METHOD_STARTED_AT_RANGE branch, where the Session-Id trailer grep
    is a proven-unreliable signal (that unreliability is exactly why this
    branch is reached).  Uses ``git rev-list --count`` over candidate_range
    rather than trailer-grep.  Returns 0 on any NON-timeout git failure
    (graceful-absent, unchanged pre-existing contract).

    Returns `None` — NOT `0` — on a `_git_run` TIMEOUT (Review: EM
    disposition on code-reviewer F1's live demonstration,
    test_c3_trailerless_large_range_scores_partition_mandatory: this
    function feeds the review-scale brightline directly, and `0` reads as
    "measured small" — the opposite safety direction from "could not
    measure". The caller (`_resolve_branches`' Branch 8) is the ONE place
    that consumes this; it must coalesce `None` to a concrete int for the
    receipt-facing field (never let `None` itself reach a `>=` comparison or
    a JSON payload) while still forcing `partition_mandatory` from the
    `None`, not from the coalesced value.

    `warnings`: optional out-param — a `_git_run` TIMEOUT also appends one
    entry here, in addition to the `None` return, so a caller wanting the
    human-readable reason (not just the fact) has a signal to read. Same
    "fold scan_errors into your own evidence" contract
    `_find_all_consumed_handoffs` already documents.
    """
    if not candidate_range:
        return 0
    result = _git_run(["rev-list", "--count", candidate_range], cwd=worktree_root)
    if result.returncode != 0:
        _record_git_timeout(result, "_range_commit_count", warnings)
        return None if result.returncode == _GIT_TIMEOUT_RETURNCODE else 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        print(f"skip: _range_commit_count: return int(result.stdout.strip()) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 0


def _range_diff_loc(
    worktree_root: Path, candidate_range: str, *, warnings: list[str] | None = None
) -> int | None:
    """Approximate total lines-of-change within candidate_range.

    Range-scoped counterpart to ``_session_diff_loc`` for the
    SCOPING_METHOD_STARTED_AT_RANGE branch.  Uses ``git log --stat`` over
    candidate_range (not trailer-grep) and sums insertions + deletions from
    the summary lines, same parsing shape as ``_session_diff_loc``.  Returns 0
    on any NON-timeout failure (graceful-absent, unchanged pre-existing
    contract).

    Returns `None` — NOT `0` — on a `_git_run` TIMEOUT. See
    `_range_commit_count`'s docstring for why, and for the caller's
    obligation to coalesce `None` before it reaches a comparison or payload.

    `warnings`: see `_range_commit_count`'s docstring — same degrade-loud
    out-param, appended only on a `_git_run` timeout.
    """
    if not candidate_range:
        return 0
    result = _git_run(["log", "--stat", "--format=", candidate_range], cwd=worktree_root)
    if result.returncode != 0:
        _record_git_timeout(result, "_range_diff_loc", warnings)
        return None if result.returncode == _GIT_TIMEOUT_RETURNCODE else 0
    total = 0
    for line in result.stdout.splitlines():
        # Summary lines look like: "2 files changed, 47 insertions(+), 12 deletions(-)"
        if "changed" in line:
            insertions = sum(int(m) for m in re.findall(r"(\d+) insertion", line))
            deletions = sum(int(m) for m in re.findall(r"(\d+) deletion", line))
            total += insertions + deletions
    return total


def _range_touched_paths(
    worktree_root: Path, candidate_range: str, *, warnings: list[str] | None = None
) -> list[str] | None:
    """Return file paths touched within candidate_range.

    Range-scoped counterpart to ``_session_touched_paths`` for the
    SCOPING_METHOD_STARTED_AT_RANGE branch.  Uses ``git log --name-only`` over
    candidate_range (not trailer-grep).  Deduped, order-preserving.  Returns
    [] on any NON-timeout git failure (graceful-absent, unchanged
    pre-existing contract).

    Returns `None` — NOT `[]` — on a `_git_run` TIMEOUT. See
    `_range_commit_count`'s docstring for why: `[]` feeds
    `_session_surface_count`, which would silently read as "0 surfaces
    touched" if a `None` were not coalesced first at the call site.

    `warnings`: see `_range_commit_count`'s docstring — same degrade-loud
    out-param, appended only on a `_git_run` timeout.
    """
    if not candidate_range:
        return []
    result = _git_run(
        ["log", "--name-only", "--format=", candidate_range],
        cwd=worktree_root,
    )
    if result.returncode != 0:
        _record_git_timeout(result, "_range_touched_paths", warnings)
        return None if result.returncode == _GIT_TIMEOUT_RETURNCODE else []
    seen: set[str] = set()
    paths: list[str] = []
    for line in result.stdout.splitlines():
        p = line.strip()
        if p and p not in seen:
            seen.add(p)
            paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# Scoping-analysis helpers (C1 — trailer-reliability, started_at range,
# foreign-commit + contiguity detection)
#
# PURE helpers only: git reads, no receipt mutation, no X-node emission.  Not
# yet wired into the B-wave scoping site (:1321-1341) — that wiring is C3.
#
# Spec backlink:
#   docs/plans/2026-07-12-wsc-concurrent-tree-safety-hardening.md § Tasks C1
#   cross-repo/archive/2026-07-12-example-retrieval-repo-em-wsc-concurrent-tree-race.md
# ---------------------------------------------------------------------------

#: scoping_method enum values — mirrors the C2 receipt_schema additive field.
SCOPING_METHOD_TRAILER = "session_id_trailer"
SCOPING_METHOD_STARTED_AT_RANGE = "started_at_contiguous_range"
SCOPING_METHOD_AMBIGUOUS = "ambiguous-x-node"


@dataclass
class ScopingVerdict:
    """Structured outcome of the C1 scoping-analysis pipeline.

    Attributes:
      method: one of SCOPING_METHOD_TRAILER / SCOPING_METHOD_STARTED_AT_RANGE /
        SCOPING_METHOD_AMBIGUOUS — which scoping strategy the caller should use.
      foreign_count: number of commits in candidate_range attributed to a
        DIFFERENT session (trailer mismatch, or trailerless-but-out-of-scope-paths).
      contiguous: True iff the true session commit set is an unbroken suffix
        of candidate_range (no foreign commit interleaved between session
        commits or after the last session commit up to HEAD).
      candidate_range: the started_at-bounded git revision range string
        (e.g. "<first-sha>^..HEAD"), or "" when it could not be derived.
      warnings: one entry per `_git_run` TIMEOUT hit while computing this
        verdict's own `_trailer_reliable` / `_started_at_candidate_range`
        reads (Review: code-reviewer F1) — empty on a clean read. Mirrors
        `resolver.py`'s `detect_git_provenance_consumed` degrade-loud
        contract: a non-empty list here means `method`/`contiguous` were
        computed under a fail-open default, not confirmed by a completed git
        read, and the caller MUST fold this into its own evidence rather than
        trusting the verdict as if the reads all succeeded.

    Not yet wired into the receipt (C3) — this is the pure analysis result.
    """

    method: str
    foreign_count: int
    contiguous: bool
    candidate_range: str
    warnings: list[str] = field(default_factory=list)


def _trailer_reliable(
    worktree_root: Path,
    sid: str,
    started_at: str | None,
    *,
    warnings: list[str] | None = None,
) -> bool:
    """Return whether the Session-Id trailer is a trustworthy scoping signal for sid.

    UNRELIABLE (returns False) when the trailer grep finds zero matches
    (``session_commit_count_attributed`` reports ``value: 0``, computed) BUT HEAD has moved since the session's
    own ``started_at`` — i.e. the session did real work but none of its
    commits carry the trailer (plain ``git add -- … && git commit``,
    SC-DR-008 baseline, carries no ``Session-Id:`` trailer).

    RELIABLE (returns True) when either at least one trailer-tagged commit
    exists, OR started_at is absent/unparseable (nothing to compare HEAD
    against — trailer absence is not distinguishable from "no work happened",
    so we do not downgrade reliability on that basis alone).

    Pure read — no receipt mutation, no X-node emission.

    `warnings`: see `_range_commit_count`'s docstring — same degrade-loud
    out-param.

    TIMEOUT ON THE HEAD-MOVED CHECK RETURNS FALSE, NOT TRUE (Review: EM
    disposition on code-reviewer F1's live demonstration,
    test_c3_trailerless_large_range_scores_partition_mandatory). "Could not
    tell whether HEAD moved" is INDETERMINATE, not "nothing moved" — and the
    two have opposite safety consequences here: True keeps the (already-
    proven-unreliable-by-construction, since this line is only reached when
    the trailer grep found zero matches) trailer-grep numbers authoritative,
    False routes the caller to the range-recompute/foreign-commit-check
    pipeline, which itself fails toward SCOPING_METHOD_AMBIGUOUS (forces an
    EM-supplied commit set) whenever ITS OWN reads are also indeterminate —
    see `_started_at_candidate_range`'s docstring for why an empty range on
    timeout already lands there safely with no further change needed. A
    non-timeout git failure (git missing, not a repo) keeps the pre-existing
    `True` — that failure class is unchanged from before this fix and stays
    out of scope, per this function's other preexisting-fail-open note above.
    """
    # Preexisting fail-open behaviour at this in-module call site is left unchanged by
    # the C2 sub-task (docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md):
    # a degraded read is treated the same as a computed `value: 0` here, same as the
    # old bare-int `_session_commit_count` did. The new degraded-with-evidence
    # distinction is surfaced by the facade (coordinator_core/session/session_facts.py),
    # not retrofitted onto this pre-existing ceremony branch.
    commit_count_record = session_commit_count_attributed(worktree_root, sid)
    if not commit_count_record["degraded"] and commit_count_record["value"] > 0:
        return True

    if started_at is None:
        return True

    try:
        started_epoch = datetime.fromisoformat(
            started_at.replace("Z", "+00:00")
        ).timestamp()
    except (ValueError, AttributeError):
        print(f"skip: _trailer_reliable: started_epoch = datetime.fromisoformat( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return True

    result = _git_run(["log", "-1", "--format=%ct"], cwd=worktree_root)
    if result.returncode != 0:
        if result.returncode == _GIT_TIMEOUT_RETURNCODE:
            _record_git_timeout(result, "_trailer_reliable", warnings)
            return False
        return True
    head_ct_str = result.stdout.strip()
    if not head_ct_str:
        return True
    try:
        head_epoch = float(head_ct_str)
    except ValueError:
        print(f"skip: _trailer_reliable: head_epoch = float(head_ct_str) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return True

    if head_epoch > started_epoch:
        return False
    return True


def _started_at_candidate_range(
    worktree_root: Path,
    started_at: str | None,
    *,
    warnings: list[str] | None = None,
) -> str:
    """Derive the started_at-bounded candidate commit range for scoping.

    Returns "<first-commit-after-started_at>^..HEAD" — the first commit whose
    author date is STRICTLY AFTER started_at, through HEAD.  Returns "" when
    started_at is absent/unparseable, or when no commit exists after
    started_at (nothing to scope).

    Deliberate divergence (mirrors the :1455-1467 note on
    _scan_session_scratch): candidate_range is derived from the session's own
    started_at sentinel, NOT git's very first commit in the repo — the first
    commit in the repo is almost never this session's boundary on a shared
    long-lived branch.

    `warnings`: see `_range_commit_count`'s docstring — same degrade-loud
    out-param. The `""` this returns on a timeout is deliberately left
    indistinguishable from "nothing to scope" by return value alone — unlike
    the three `_range_*` helpers (which return `None` on a timeout, see
    `_range_commit_count`'s docstring), this function's return value never
    needs that sentinel. Audited (EM disposition on code-reviewer F1):
    `analyze_session_scoping` only branches on a non-empty `candidate_range`
    when `_trailer_reliable` has ALREADY returned False (trailer proven
    unreliable) — an empty range at that point, timeout or genuine, forces
    `SCOPING_METHOD_AMBIGUOUS` (the EM-supplied-scope path, the safe
    default) rather than `SCOPING_METHOD_STARTED_AT_RANGE`. When
    `_trailer_reliable` returned True instead, this function's return value
    only ever reaches the receipt as an audit-trail `sha_range` string — it
    never feeds a brightline/partition computation on that path (the trailer
    branch uses `_session_diff_loc`/`session_commit_count_attributed`
    instead) — so a timed-out "" there is inert, not unsafe.
    """
    if started_at is None:
        return ""
    try:
        # Validate parseability; git's --since does its own date parsing but
        # we reject unparseable started_at up front for a consistent contract
        # with the other C1 helpers.
        datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        print(f"skip: _started_at_candidate_range: datetime.fromisoformat(started_at.replace(\"Z\", \"+00:00\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""

    result = _git_run(
        ["log", "--since", started_at, "--format=%H", "--reverse"],
        cwd=worktree_root,
    )
    if result.returncode != 0:
        _record_git_timeout(result, "_started_at_candidate_range", warnings)
        return ""
    shas = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not shas:
        return ""
    first_sha = shas[0]
    return f"{first_sha}^..HEAD"


def _detect_foreign_commits(
    worktree_root: Path,
    sid: str,
    candidate_range: str,
    known_scope_paths: frozenset[str],
) -> list[str]:
    """Return the SHAs within candidate_range NOT attributable to sid.

    Thin wrapper over `session_attribution.detect_foreign_commits` (the
    shared two-consumer classification module — see docs/plans/2026-07-27-
    review-trail-scope-guard.md § C1). Kept here, under this file's own
    private name, because this module's test suite imports it directly; the
    classification algorithm itself now lives exactly once, in
    `session_attribution`.
    """
    return session_attribution.detect_foreign_commits(
        worktree_root, sid, candidate_range, known_scope_paths,
    )


def _range_is_contiguous_suffix(
    worktree_root: Path,
    candidate_range: str,
    foreign_shas: list[str],
) -> bool:
    """Return True iff no foreign commit is interleaved within candidate_range.

    Thin wrapper over `session_attribution.range_is_contiguous_suffix` — see
    `_detect_foreign_commits`'s docstring for why this wrapper exists.
    """
    return session_attribution.range_is_contiguous_suffix(
        worktree_root, candidate_range, foreign_shas,
    )


def analyze_session_scoping(
    worktree_root: Path,
    common_dir: Path,
    sid: str,
    known_scope_paths: frozenset[str] | None = None,
) -> ScopingVerdict:
    """Run the full C1 scoping-analysis pipeline and return a ScopingVerdict.

    Composes the four pure helpers above:
      1. _trailer_reliable — is the Session-Id trailer a trustworthy signal?
      2. _started_at_candidate_range — the started_at-bounded git range.
      3. _detect_foreign_commits — foreign SHAs within that range.
      4. _range_is_contiguous_suffix — is the true session set an unbroken
         suffix of the range?

    method selection:
      - Trailer reliable (at least one Session-Id-tagged commit, or no work
        happened since started_at) ⇒ SCOPING_METHOD_TRAILER — the existing
        grep-based scoping (session_commit_count_attributed / _session_diff_loc) stays
        authoritative; C3 wires this branch to a no-op (keep current path).
      - Trailer unreliable AND the started_at range is foreign-free and
        contiguous ⇒ SCOPING_METHOD_STARTED_AT_RANGE — the range itself is
        the recovered scope.
      - Trailer unreliable AND the range is either non-derivable, contains
        foreign commits, or is non-contiguous ⇒ SCOPING_METHOD_AMBIGUOUS —
        caller (C3) must force an EM-supplied commit set via an X-node.

    Not yet wired into the receipt (C3 does that) — this is the pure decision
    function, callable in isolation for both wiring and unit testing.

    Every `_git_run` TIMEOUT hit by `_trailer_reliable` /
    `_started_at_candidate_range` while computing this verdict is collected
    into the returned `ScopingVerdict.warnings` (Review: code-reviewer F1) —
    see that field's docstring for why a caller must not treat a non-empty
    list as an ordinary computed result.
    """
    if known_scope_paths is None:
        known_scope_paths = frozenset()

    warnings: list[str] = []
    started_at = _read_started_at(common_dir, sid)
    candidate_range = _started_at_candidate_range(worktree_root, started_at, warnings=warnings)

    if _trailer_reliable(worktree_root, sid, started_at, warnings=warnings):
        return ScopingVerdict(
            method=SCOPING_METHOD_TRAILER,
            foreign_count=0,
            contiguous=True,
            candidate_range=candidate_range,
            warnings=warnings,
        )

    foreign_shas = _detect_foreign_commits(
        worktree_root, sid, candidate_range, known_scope_paths
    )
    contiguous = _range_is_contiguous_suffix(worktree_root, candidate_range, foreign_shas)

    if candidate_range and not foreign_shas and contiguous:
        method = SCOPING_METHOD_STARTED_AT_RANGE
    else:
        method = SCOPING_METHOD_AMBIGUOUS

    return ScopingVerdict(
        method=method,
        foreign_count=len(foreign_shas),
        contiguous=contiguous,
        candidate_range=candidate_range,
        warnings=warnings,
    )




def _sanitize_consumed_handoffs(
    worktree_root: Path,
    sid: str,
    consumed_handoffs_all: list[tuple[str, dict[str, Any]]],
    *,
    operator_asserted: bool = False,
    operator_asserted_paths: Optional[list[str]] = None,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Final containment+ownership gate on the merged consumed-handoff set.

    Defense-in-depth choke point (foreign-repo bleed defect, 2026-07-13 cockpit
    incident): the per-source scalar guard in _resolve_branches already rejects a
    foreign or peer-owned pickup.handoff, but the plural ``consumed_handoffs_all``
    set is assembled from MULTIPLE sources (session-shape pickup path, archive-aware
    scan UNION, grep-fallback append) and then flows UN-REVERIFIED into both the
    STEP_0 receipt evidence and ctx.consumed_handoffs → STEP_2_7's stamp target.
    A single check on one source can be bypassed by a different source or a future
    refactor; enforcing the invariant HERE — at the one point every source
    converges — makes the bleed structurally impossible rather than merely absent
    on the paths tested today.

    Invariant (drops any entry that violates it):
      1. Containment — the path must resolve INSIDE worktree_root
         (_resolve_in_repo rejects absolute paths and ../ traversal that escape
         to a foreign repo).  Entries that pass are re-expressed as repo-RELATIVE
         POSIX strings so no absolute foreign prefix and no relativized phantom
         (foreign basename joined to worktree_root) can survive downstream.
         NEVER bypassable — not even by ``operator_asserted`` (see below) — this
         is the 2026-07-13 foreign-repo-bleed defense and stays absolute.
      2. Existence — the path must exist on disk. Also never bypassable.
      3. Ownership — the handoff's own frontmatter ``consumed_by`` must equal sid
         (anchored via _get_handoff_consumed_by), so a temporally-adjacent peer's
         in-repo handoff is never mis-stamped as this session's predecessor.
         ``operator_asserted=True`` (env-override callers ONLY — see
         coordinator_core.ops.ceremony.wsc_disposition's escalate-only override
         contract) bypasses ONLY this third half: an operator naming a handoff
         via WSC_CONSUMED_HANDOFF is making the ownership assertion manually,
         for exactly the cases a ``consumed_by`` stamp cannot express (the
         claiming session crashed/is dead — Detector C indeterminate/ambiguous
         — or the handoff was archived without ever having a live consume
         stamp — ship-then-archive). An entry kept via this bypass is appended
         to ``operator_asserted_paths`` (when the caller supplies a list) so
         the caller can record the bypass loudly in the receipt — see
         _resolve_branches' consumed_handoff_ownership_operator_asserted /
         env_override_diagnostics NOTE. Never let a bypassed entry look like a
         normally-verified one downstream.

    Returns (kept, rejected_paths) — kept entries carry the repo-relative path;
    rejected_paths preserves the ORIGINAL (pre-sanitization) string of each
    dropped entry for evidence/receipt visibility.

    Negative-spec: do NOT relax the containment or existence halves for ANY
    caller, operator_asserted or not — the whole point of the 2026-07-13
    incident is that a foreign file both existed AND declared consumed_by:sid;
    containment is what rejects it, existence is what rejects a phantom path,
    and only the ownership half is the operator's to assert manually.
    """
    kept: list[tuple[str, dict[str, Any]]] = []
    rejected: list[str] = []
    seen_rel: set[str] = set()
    for path, fm in consumed_handoffs_all:
        hf_in_repo = _resolve_in_repo(worktree_root, path)
        if hf_in_repo is None or not hf_in_repo.exists():
            rejected.append(path)
            continue
        owned = _get_handoff_consumed_by(str(hf_in_repo)) == sid
        if not owned and not operator_asserted:
            rejected.append(path)
            continue
        rel = hf_in_repo.relative_to(worktree_root.resolve()).as_posix()
        if rel in seen_rel:
            continue
        seen_rel.add(rel)
        kept.append((rel, fm))
        if not owned and operator_asserted_paths is not None:
            operator_asserted_paths.append(rel)
    return kept, rejected




# ---------------------------------------------------------------------------
# STEP_2_65C flip half — routes through memo.transition's resolve verb (C1/C2)
# ---------------------------------------------------------------------------
# Spec backlink: pln-give-the-memo-disposition-flip-e580c2 § C2
#
# Structural note: this is NOT called from _resolve_branches / the
# ceremony.wsc_resolve op handler below. STEP_2_65B is a J-node — its answer
# (the per-memo disposition list) does not exist until the EM fills it during
# the EM turn, AFTER wsc_resolve's single Phase-1 pass has already run and
# emitted its receipt. resolve_named_memo_dispositions is exposed here as a
# real, independently-callable, independently-tested op-routing seam — the
# same relationship _tail_archive_memos (wsc_commit.py) has to STEP_2_65C's
# sweep half, which is likewise declared (not executed) inside
# _resolve_branches and actually runs from the Phase-2 D-tail once EM answers
# are available. Wiring THIS function into that same D-tail is follow-on
# integration work belonging to whichever chunk touches wsc_commit.py; no
# chunk in the C2 executing plan's task list does, so it is not done here.


async def _call_memo_resolve(
    memo_path: str, session_id: str, at: str, disposition_params: dict[str, Any],
) -> dict[str, Any]:
    """Seam: invoke memo.transition's ``resolve`` verb (C1) via the op registry.

    Patched in tests (same seam pattern as wsc_commit.py's ``_tail_archive_memos``).
    """
    import coordinator_core.ops.memo_transition  # noqa: F401 — trigger op registration
    # `memo.transition` is not on the suspension roster today, but
    # `get_op_handler` raises `OpSuspendedError` rather than returning None
    # for any op that IS suspended (ipc.py's own docstring) -- fold that
    # raise into this existing not-registered branch so a future kill of
    # this op degrades to the same reported failure instead of crashing.
    try:
        handler = get_op_handler("memo.transition")
    except OpSuspendedError:
        handler = None
    if handler is None:
        return {"exit_code": 1, "applied": False, "error": "memo.transition op not registered"}
    params = {"verb": "resolve", "memo": memo_path, "session_id": session_id, "at": at}
    params.update(disposition_params)
    return await handler(params, repo_root=None)


async def resolve_named_memo_dispositions(
    worktree_root: Path,
    open_memos: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    *,
    session_id: str,
    at: str,
) -> list[dict[str, Any]]:
    """Give STEP_2_65C's flip half a real op: for each memo STEP_2_65B's
    (widened, C2) judgment named, issue a ``memo.transition resolve`` call
    carrying THAT memo's disposition.

    A memo NOT named in ``dispositions`` is left untouched — no resolve call
    is issued for it, and its on-disk ``status`` stays ``open``.

    Each item in ``dispositions`` is a dict shaped:
      {"memo": <path as recorded in open_memos>, "bulk": bool (optional),
       "decision"|"actioned_note": ..., "decision_note"/"realized_by": ...,
       "distill_fate"/"in_repo_capture": ... (optional)}

    ``bulk: true`` requests the cheap no-action-needed fast path (AC4a) — this
    function enforces the eligibility gate (via the SAME ``bulk_eligible``
    verdict ``_scan_open_memos`` already computed for the named memo) BEFORE
    issuing any resolve call; an ineligible bulk request is refused fail-loud
    without ever reaching ``memo.transition``. ``bulk`` does not exempt the
    disposition itself from validation — ``resolve`` still requires exactly
    one of decision/actioned_note (``_validate_action_disposition``); bulk
    only widens WHO may supply a generic no-action-needed note without
    individual per-memo reasoning.

    A ``dispositions`` entry naming a memo that is not in ``open_memos`` (a
    stale or hallucinated judgment answer) is refused fail-loud rather than
    silently skipped or silently resolved against an arbitrary path.

    Returns one result dict per ``dispositions`` entry, each carrying
    ``memo`` plus either the ``memo.transition`` result shape
    ({exit_code, applied, message} or {exit_code, applied, error}) or a
    same-shaped local refusal.
    """
    by_path = {m.get("path"): m for m in open_memos}
    results: list[dict[str, Any]] = []
    for item in dispositions:
        memo_rel = str(item.get("memo") or "").strip()
        record = by_path.get(memo_rel)
        if record is None:
            results.append({
                "memo": memo_rel, "exit_code": 1, "applied": False,
                "error": (
                    f"resolve refused: {memo_rel!r} is not one of the open memos "
                    "STEP_2_65A enumerated this session"
                ),
            })
            continue

        if item.get("bulk"):
            eligible = record.get("bulk_eligible", False)
            if not eligible:
                results.append({
                    "memo": memo_rel, "exit_code": 1, "applied": False,
                    "error": f"bulk disposition refused: {record.get('bulk_reason', '')}",
                })
                continue

        disposition_params = {
            k: item.get(k)
            for k in (
                "decision", "decision_note", "realized_by", "actioned_note",
                "distill_fate", "in_repo_capture",
            )
            if item.get(k) is not None
        }
        memo_abs = str(worktree_root / memo_rel)
        result = await _call_memo_resolve(memo_abs, session_id, at, disposition_params)
        results.append({"memo": memo_rel, **result})
    return results


