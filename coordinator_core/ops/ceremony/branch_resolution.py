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
outright) — no live caller ever dispatched it as an op. This module survives
under its current name because ``ceremony.session_instructions`` (see that
module's docstring) imports ``_read_session_shape`` / ``_resolve_branches`` /
``_session_commit_log`` directly — the engine is live, only the op registration
was dead. The retired handler's BODY survives too, undecorated and renamed to
``resolve_session_branches`` — it is the sole end-to-end integration surface
``tests/test_branch_resolution.py``'s ~4,450 lines exercise (11 call sites,
invoked as a plain function, never through op dispatch); see that function's
own docstring for why dropping only the decorator was the right trade.

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
    detect_git_provenance_consumed,
    find_all_consumed_handoffs as _find_all_consumed_handoffs,
    resolve_in_repo as _resolve_in_repo,
)
from coordinator_core.ops.ceremony.wsc_disposition import (
    PREDECESSOR_CONSUMED,
    SINGLE_SESSION,
    WRITE_TOKEN,
    canonicalize,
    normalize_override_handoff,
    resolve_env_override,
)
from coordinator_core.ops.ceremony.node_handlers import (
    # Step constants
    STEP_0,
    STEP_1A,
    STEP_1B,
    STEP_1C,
    STEP_1_2,
    STEP_2A,
    STEP_2B,
    STEP_2_4A,
    STEP_2_4B,
    STEP_2_6_1,
    STEP_2_6_2,
    STEP_2_6_3,
    STEP_2_6_3A,
    STEP_2_6_4,
    STEP_2_6_5,
    STEP_2_6_5A,
    STEP_2_6_6A,
    STEP_2_6_6B,
    STEP_2_6_6C,
    STEP_2_6_7,
    STEP_2_6_8,
    STEP_2_65A,
    STEP_2_65B,
    STEP_2_65C,
    STEP_2_67A,
    STEP_2_67B,
    STEP_2_7,
    STEP_2_8A,
    STEP_2_8B,
    STEP_2_8C,
    STEP_2_9A,
    STEP_2_9B,
    STEP_B1,
    STEP_2_9C,
    STEP_2_9D,
    STEP_2_9E,
    STEP_2_9_OBS,
    STEP_2_95A,
    STEP_2_95B,
    STEP_2_96,
    STEP_3_0,
    STEP_3_1,
    STEP_3_2,
    STEP_3_3,
    STEP_3_5A,
    STEP_3_5B,
    STEP_4A,
    STEP_4B,
    # Handlers
    emit_b,
    emit_f,
    emit_j,
    # Review: code-reviewer F1 — emit_x removed; STEP_2_67A's graceful-negative uses
    # make_x_node directly (emit_x raises KeyError now that X_MISSING_SIGNALS is empty).
    handle_d,
    # C2 — bulk-eligibility classifier for STEP_2_65B's widened disposition contract.
    classify_bulk_eligibility,
)
from coordinator_core.ops.ceremony.pipeline_context import (
    BRANCH_ID_BIG_DIFF_BRIGHTLINE,
    BRANCH_ID_CHAIN_SLUG,
    BRANCH_ID_CHECKLIST_ITEMS,
    BRANCH_ID_COMPLETENESS_CHECKLIST,
    BRANCH_ID_DOC_FRAGILE_DOMAIN,
    BRANCH_ID_GOVERNING_PLAN,
    BRANCH_ID_IDEMPOTENCY_GUARD,
    BRANCH_ID_LESSON_QUALIFIES,
    BRANCH_ID_LESSON_UNIVERSAL,
    BRANCH_ID_LOE_PATH,
    BRANCH_ID_MEMOS_RESOLVED,
    BRANCH_ID_NATURE_CLASSIFICATION,
    BRANCH_ID_OPEN_MEMOS,
    BRANCH_ID_PLAN_CLAIM_GUARD,
    BRANCH_ID_REVIEW_WAVE_SCALE,
    BRANCH_ID_SESSION_AUTHORED_FILES,
    BRANCH_ID_WSC_DISPOSITION,
    BranchResolution,
    PipelineContext,
)
from coordinator_core.ops.ceremony.receipt_emit import emit_receipt
from coordinator_core.ops.ceremony.receipt_schema import make_x_node
from coordinator_core.ops.completion_nature import classify_nature
from coordinator_core.ops.fleet._common import main_worktree_root, rel_id
from coordinator_core.ops.fleet._memo_compose import _normalize_in_reply_to

log = logging.getLogger(__name__)

# Step IDs applicable ONLY when disposition == "chain-terminal" — used to compute
# the declared applicable_node_ids membership list (op-spec §3, Option B).  Two
# distinct reasons land a step here:
#   - STEP_2_7 / STEP_2_9C: their resolving_op is skipped outright in
#     single-session (each carries evidence.chain_terminal below, in _resolve_branches).
#   - STEP_2_96: reads state/tasks/<sid>/completeness-checklist.yaml, a mirror that
#     only exists post-pickup (written by /pickup Step 5.5) — structurally absent
#     in single-session, so the node is a guaranteed no-op there (see Branch 13
#     comment: "normal single-session case, the mirror is absent").  STEP_2_96 does
#     NOT carry an evidence.chain_terminal flag of its own; membership here is a
#     declared semantic exclusion, not a scavenged evidence read.
# Review: code-reviewer (690dd6f9) -- STEP_2_75 dropped: its only ctx.add_node
# call site was removed by the handoff-tracker retirement diff, so the id can
# never appear in the ledger and the prior membership was stale.
_CHAIN_TERMINAL_ONLY_STEPS: frozenset[str] = frozenset(
    {STEP_2_7, STEP_2_9C, STEP_2_96}
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Partition-mandatory thresholds (mirrors review-brightline-gate.sh defaults;
# Port of: review-brightline-gate.sh, DoE b5a4192c, 2026-07-20)
_BRIGHTLINE_LOC = 500
_BRIGHTLINE_COMMITS = 5
_BRIGHTLINE_SURFACES = 4

# Doc-fragile file extensions (coordinator.local.md project_subtypes = ue/unity/godot)
_DOC_FRAGILE_EXTENSIONS: frozenset[str] = frozenset({
    ".uasset", ".umap", ".blend", ".unity", ".prefab",
    ".scene", ".godot", ".tres", ".tscn",
})


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
# L1b grep fallback — disposition from handoff grep
# ---------------------------------------------------------------------------


def _grep_disposition(worktree_root: Path, sid: str) -> tuple[str, list[str], list[str]]:
    """Resolve WSC_DISPOSITION when session-shape.json / pickup absent.

    Despite the name, the disposition (and the handoff paths returned) are
    derived from the anchored ``_find_all_consumed_handoffs`` match set — NOT
    from a raw ``git grep`` hit list.  Returns
    (disposition, consumed_handoff_paths, scan_errors) where
    consumed_handoff_paths carries EVERY anchored match (not just the
    first — a DAG-pickup session can own N predecessor handoffs even when
    session-shape.json is absent and this fallback is the only resolution site)
    and scan_errors carries one entry per unreadable handoffs subtree the scan
    could not walk (empty when the scan completed cleanly).
      disposition = "chain-terminal" when at least one anchored match is found
      disposition = "single-session" when no anchored match is found

    This is the L1b Migration Path from session-state-contract.md — it is the
    fallback for sessions started before the session-shape.json L1 producer landed.

    Review: code-reviewer F1 — previously ran its own unanchored
    ``git grep "consumed_by: {sid}" state/handoffs/`` and returned the raw hit
    list, matching body-prose mentions (not just frontmatter) and never
    scanning archive/handoffs/ — the exact defect class the diff's own
    negative-spec on _find_consumed_handoff warns against, surviving at this
    sibling call site.  Now routes through _find_all_consumed_handoffs so L1b
    agrees with the primary session-shape path on both the anchored-match
    definition and the archive-scan capability.  Negative-spec: do NOT
    reintroduce a bare git-grep-derived path here — see
    _find_all_consumed_handoffs's own negative-spec.

    A non-empty scan_errors here means "single-session" is NOT a confirmed
    negative — a predecessor handoff could be sitting under the unreadable
    subtree.  The caller records scan_errors in step0_evidence rather than
    letting a "single-session" disposition read as clean when it is really
    "could not fully rule out chain-terminal" (see call site).
    """
    scan_errors: list[str] = []
    matches = _find_all_consumed_handoffs(worktree_root, sid, scan_errors=scan_errors)
    if matches:
        return WRITE_TOKEN, [path for path, _fm in matches], scan_errors
    return SINGLE_SESSION, [], scan_errors


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


# ---------------------------------------------------------------------------
# Governing plan discovery (D-readable part)
# ---------------------------------------------------------------------------


def _list_candidate_plans(worktree_root: Path, sid: str) -> list[str]:
    """Return repo-relative paths for candidate governing plan docs.

    D-readable: scans docs/plans/ for .md files.  The J-node asks the EM which
    plan (if any) was the subject of this session — that requires session memory.
    The D-node produces evidence (list of candidate files); the J-node follows.
    """
    plans_dir = worktree_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []
    return sorted(rel_id(p, worktree_root) for p in plans_dir.glob("*.md"))


# _resolve_in_repo and _find_all_consumed_handoffs are promoted to a public
# contract in coordinator_core.ops.ceremony.resolver (C5,
# docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C5) and imported
# above as aliases — both wsc_resolve.py (this module) and the wsc_tail
# rebuild's consumed_handoff_stamp.py now share the single canonical
# implementation instead of the tail privately reaching into this module.


def _find_consumed_handoff(
    worktree_root: Path,
    sid: str,
    *,
    scan_errors: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (handoff_path, frontmatter) for the FIRST handoff consumed by sid.

    Thin single-match wrapper over ``_find_all_consumed_handoffs`` — kept for
    call sites that only need one representative predecessor (e.g. chain-slug
    resolution). Returns ("", {}) when not found.

    scan_errors: optional out-param forwarded to ``_find_all_consumed_handoffs``
    — see that function's docstring. A caller passing this in must not treat
    a ("", {}) result as a confirmed "sid consumed nothing" when scan_errors
    comes back non-empty (see _resolve_branches's recovery-scan call site).

    Negative-spec (DEC-5 guard,
    docs/plans/2026-07-24-multibaton-pickup-and-args-prose.md § C3): do NOT
    call this on a chain-terminal path. A session can own N consumed
    handoffs deriving from N distinct predecessors (and, past C3, N distinct
    origin stubs) — truncating to the first match via `matches[0]` is
    exactly the head-1 defect DEC-5 removed from `post_commit_tail.py`'s
    origin-stub-close step. `_resolve_branches`'s own chain-terminal
    disposition path (see its inline "Review: code-reviewer Finding 3" note
    a few lines below its own `_find_all_consumed_handoffs` call) already
    does NOT call this wrapper — it derives `consumed_handoff_path` from the
    same anchored `_find_all_consumed_handoffs` scan_group directly. Keep it
    that way: this wrapper survives ONLY for call sites that provably need
    one representative predecessor (chain-slug resolution), never a
    disposition/consumed-set/origin-stub-close path. A future chain-terminal
    call site reaching for this function is the bug this note exists to
    catch in review.
    """
    matches = _find_all_consumed_handoffs(worktree_root, sid, scan_errors=scan_errors)
    return matches[0] if matches else ("", {})


def _dedup_handoffs_by_relpath(
    *groups: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, dict[str, Any]]]:
    """Merge handoff (path, frontmatter) groups, deduped by normalized relpath.

    Groups are concatenated in argument order and de-duplicated on first
    occurrence, so callers control precedence by group ordering (earlier
    group wins the surviving slot — e.g. pickup-path result inserted FIRST so
    consumed_handoffs[0] matches the existing scalar-fallback ordering).
    Guards the UNION between independently-sourced but overlapping scans
    (e.g. the session-shape.pickup path and the archive-aware
    _find_all_consumed_handoffs scan can both surface the same handoff) from
    double-counting the same file as two entries.

    Review: code-reviewer (Slice C2, Finding 1) — the dedup key is
    ``str(Path(path))`` string form only: it normalizes ``./`` prefixes and
    separator style but does NOT resolve ``..`` segments, does NOT case-fold
    (relevant on case-insensitive filesystems like macOS default APFS/HFS+),
    and does not canonicalize symlinks. Adequate for the realistic input
    shape (producer-written or glob-derived repo-relative POSIX strings with
    no ``..`` or case variance) but NOT a fully-normalized-path guarantee.
    """
    seen: set[str] = set()
    merged: list[tuple[str, dict[str, Any]]] = []
    for group in groups:
        for path, fm in group:
            key = str(Path(path))
            if key in seen:
                continue
            seen.add(key)
            merged.append((path, fm))
    return merged


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


def _sanitize_detector_b_hits(
    worktree_root: Path,
    consumed_handoffs: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """Containment-only gate on Detector B (git-provenance) hits.

    Detector B (resolver.detect_git_provenance_consumed) attributes a hit via
    git's OWN provenance (a Session-Id: sid trailer on the commit that
    Added/Renamed/Copied the file) plus its own foreign-consumer/spoof guard
    on the frontmatter consumed_by — NOT via a live consumed_by: sid stamp,
    which is precisely the signal Detector B exists to substitute for (a
    ship-then-archive session that never wrote one). Reusing
    ``_sanitize_consumed_handoffs``'s ownership half here would reject every
    genuine Detector B hit outright (its whole reason to exist is an
    absent/empty consumed_by), so this sibling gate keeps ONLY the
    containment half — the same foreign-repo-bleed defense as the other
    sources, without the ownership check that does not apply to this source.

    Negative-spec: do NOT call ``_sanitize_consumed_handoffs`` for Detector B
    hits — its ownership check silently defeats every hit Detector B produces
    (2026-07-22 finding, caught by the first end-to-end
    test_wsc_resolve.py coverage for this branch: the consolidation branch
    called the ownership-checking sanitizer on a B-hit whose consumed_by was
    deliberately absent, and every hit was rejected).
    """
    kept: list[tuple[str, dict[str, Any]]] = []
    rejected: list[str] = []
    seen_rel: set[str] = set()
    for path, fm in consumed_handoffs:
        hf_in_repo = _resolve_in_repo(worktree_root, path)
        if hf_in_repo is not None and hf_in_repo.exists():
            rel = hf_in_repo.relative_to(worktree_root.resolve()).as_posix()
            if rel in seen_rel:
                continue
            seen_rel.add(rel)
            kept.append((rel, fm))
        else:
            rejected.append(path)
    return kept, rejected


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML-like key: value pairs from a markdown frontmatter block.

    Minimal parser — extracts only the --- ... --- block and returns a flat dict
    of {key: value_string}.  Does NOT parse nested YAML; not a full YAML parser.
    This is sufficient for reading status, consumed_by, chain, etc.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm


# ---------------------------------------------------------------------------
# Open-memos scanner
# ---------------------------------------------------------------------------


def _resolve_in_reply_to_target(worktree_root: Path, in_reply_to: str) -> str:
    """Classify an inbound memo's ``in_reply_to`` target as "open" | "closed" |
    "unresolvable" (C2, AC4a).

    Searches the same two directories, in the same order, and via the same
    basename normalization (``_normalize_in_reply_to``) that
    ``memo_send._validate_in_reply_to_exists`` already uses to establish this
    repo's canonical in_reply_to resolution scope — reused here rather than
    re-deriving a second search algorithm over the same two directories.

    "open"          — a file matching the basename is still sitting in
                       cross-repo/inbox/ (the ask this memo replies to has not
                       been resolved).
    "closed"        — no match in the inbox, but one exists under
                       cross-repo/archive/ (checked directly — the on-disk
                       layout is flat — and one level of subdirectory deep,
                       defensively, should a shard level ever appear) — the
                       ask was already resolved.
    "unresolvable"  — no match in either location (typo or missing target).
                       Callers MUST treat this as fail-CLOSED (AC4a case c),
                       never as equivalent to "closed".

    Narrowing (perf): cross-repo/archive/ grows monotonically (archives only
    accumulate — 1,003 files measured 2026-08-13) and this runs on every
    inbound memo carrying in_reply_to, so a full-tree rglob() filtered in
    Python scales O(archive size) for a single-basename lookup. basename is
    untrusted (memo-frontmatter-derived), so — same discipline as the rglob
    it replaces — matching stays a literal filename comparison, never a glob
    PATTERN: a direct ``archive_dir / basename`` check (today's real, flat
    layout) plus a bounded one-level ``iterdir()`` of immediate
    subdirectories (defensive shard support, cost scales with shard count
    not file count — mirrors handoff_creation_guard.find_archived_twin_by_filename's
    sharded/direct dual check, adapted since that helper uses glob() over a
    basename it trusts more than this one does).
    """
    basename = _normalize_in_reply_to(in_reply_to)
    inbox_dir = worktree_root / "cross-repo" / "inbox"
    if (inbox_dir / basename).is_file():
        return "open"
    archive_dir = worktree_root / "cross-repo" / "archive"
    if archive_dir.is_dir():
        if (archive_dir / basename).is_file():
            return "closed"
        for entry in archive_dir.iterdir():
            if entry.is_dir() and (entry / basename).is_file():
                return "closed"
    return "unresolvable"


def _scan_open_memos(worktree_root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Return (open_memos, scan_error) for open memos in cross-repo/inbox/.

    A memo is «open» when its frontmatter ``status`` field is "open" or absent
    (per the node-map: "status: open | absent").

    scan_error is None when the inbox was scanned cleanly (including the
    normal case where cross-repo/inbox/ doesn't exist), or a human-readable
    string when the inbox exists but could not be enumerated.  Callers MUST
    treat a non-None scan_error as distinct from a genuinely-empty inbox —
    open_memos == [] is ambiguous between "no open memos" and "could not
    scan"; scan_error disambiguates.

    Uses iterdir(), NOT glob("*.md") — Path.glob's selector silently
    swallows PermissionError while walking (verified: an unreadable dir →
    glob() yields an empty iterator, no exception), which previously made an
    unreadable inbox indistinguishable from a clean empty one.

    C2: each entry additionally carries ``kind``, ``in_reply_to``,
    ``bulk_target_resolution`` (``_resolve_in_reply_to_target``'s verdict, or
    ``None`` when ``in_reply_to`` is absent), ``bulk_eligible`` and
    ``bulk_reason`` (``classify_bulk_eligibility``'s verdict) — computed once
    here so both STEP_2_65B's evidence (surfaced to the EM before it answers)
    and ``resolve_named_memo_dispositions`` (the actual bulk-path gate) read
    the SAME classification rather than two independently-computed copies.
    """
    inbox = worktree_root / "cross-repo" / "inbox"
    if not inbox.is_dir():
        return [], None

    try:
        entries = sorted(inbox.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        log.warning(
            "wsc_resolve: _scan_open_memos: cannot scan %s — %s; treating as "
            "a scan failure (NOT a clean/empty inbox)",
            inbox, exc,
        )
        return [], f"{inbox}: {exc}"

    open_memos = []
    for mf in entries:
        if mf.suffix != ".md" or not mf.is_file():
            continue
        try:
            text = mf.read_text(encoding="utf-8")
        except OSError:
            print(f"skip: _scan_open_memos: text = mf.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        fm = _parse_frontmatter(text)
        status = fm.get("status", "open")  # absent = treat as open
        if status in ("open", ""):
            kind = fm.get("kind", "")
            in_reply_to = fm.get("in_reply_to") or None
            target_resolution = (
                _resolve_in_reply_to_target(worktree_root, in_reply_to)
                if in_reply_to else None
            )
            bulk_eligible, bulk_reason = classify_bulk_eligibility(
                kind, in_reply_to, target_resolution,
            )
            open_memos.append({
                "path": rel_id(mf, worktree_root),
                "status": status or "open",
                "title": fm.get("title", mf.stem),
                "kind": kind,
                "in_reply_to": in_reply_to,
                "bulk_target_resolution": target_resolution,
                "bulk_eligible": bulk_eligible,
                "bulk_reason": bulk_reason,
            })
    return open_memos, None


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


# ---------------------------------------------------------------------------
# Completeness-checklist mirror reader (C3 — STEP_2_96)
# ---------------------------------------------------------------------------


def _read_completeness_mirror(worktree_root: Path, sid: str) -> dict | None:
    """Read the completeness-checklist mirror for ``sid``.

    Reads ``worktree_root/state/tasks/<sid>/completeness-checklist.yaml``.

    Schema assertion first (AC7): checks that the file content contains the
    substring ``completeness-checklist-mirror-v1`` via a plain ``in`` check —
    NOT a YAML parse.  If a ``schema:`` line is present but does NOT contain
    ``v1``, returns ``{"schema_mismatch": <found_value>, "open_count": None}``
    so contract drift surfaces as a memo-worthy signal rather than a silent zero.

    Counts by line-pattern (NOT a YAML parser — AC7):
      open_count  = lines matching ``^\\s+state:\\s*open\\b``
      total_count = lines matching ``^\\s+state:\\b``

    Returns ``{"open_count": n, "total_count": m}`` when schema asserts and
    state: lines are present; ``None`` when the file is absent (normal case —
    no checklist ever created) or has no ``state:`` lines.

    Negative-spec: do NOT import _simple_yaml_load, _parse_frontmatter (both
    flat/scalar-only), or any YAML lib.  Line-pattern counting is exact on v1
    unquoted-scalar format (``state: open``, not ``state: "open"``), correctness
    gated by the schema-tag check.

    Spec backlink:
      docs/plans/2026-07-06-wsc-resolve-consume-doe-signals-x-to-d.md § C3
    """
    mirror_path = worktree_root / "state" / "tasks" / sid / "completeness-checklist.yaml"
    if not mirror_path.exists():
        return None
    try:
        content = mirror_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "wsc_resolve: could not read completeness-checklist for %s: %s", sid, exc
        )
        return None

    # Schema assertion: substring check — NOT a YAML parse (AC7-clean)
    if "completeness-checklist-mirror-v1" not in content:
        # Check whether a schema: line exists with a different value
        schema_val = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("schema:"):
                schema_val = stripped[len("schema:"):].strip()
                break
        if schema_val is not None:
            return {"schema_mismatch": schema_val, "open_count": None}
        # No schema: line at all — treat as absent/zero-signal
        return None

    # Line-pattern counting — NOT a YAML parser (AC7)
    # open_count: lines matching ^\s+state:\s*open\b (value = open, unquoted)
    # total_count: lines matching ^\s+state: (any value; \b after : fails for
    #   space-separated YAML "state: open" since : and space are both \W — use
    #   prefix match instead; col-0 anchor + indent requirement filters non-item lines)
    # Review: code-reviewer — hoist splitlines() to one pass (called twice before)
    lines = content.splitlines()
    open_count = sum(1 for line in lines if re.match(r"^\s+state:\s*open\b", line))
    total_count = sum(1 for line in lines if re.match(r"^\s+state:", line))
    if total_count == 0:
        return None

    return {"open_count": open_count, "total_count": total_count}


# ---------------------------------------------------------------------------
# Doc-fragile domain check
# ---------------------------------------------------------------------------


def _check_doc_fragile_domain(worktree_root: Path, sid: str) -> tuple[bool, str]:
    """Return (active, reason) for the doc-fragile domain gate.

    Checks coordinator.local.md project_subtypes and session-touched paths.
    Returns (True, reason) when both conditions hold:
      1. coordinator.local.md declares project_subtypes: [ue|unity|godot]
      2. Session-touched paths include a doc-fragile file extension

    Returns (False, reason) when either condition is absent.
    """
    cl_path = worktree_root / "coordinator.local.md"
    if not cl_path.exists():
        return False, "coordinator.local.md absent — doc-fragile domain inactive"

    try:
        cl_text = cl_path.read_text(encoding="utf-8")
    except OSError:
        print(f"skip: _check_doc_fragile_domain: cl_text = cl_path.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False, "coordinator.local.md unreadable"

    # Check for doc-fragile project_subtypes declaration
    has_fragile_subtype = any(
        kw in cl_text.lower()
        for kw in ("project_subtypes: ue", "project_subtypes: unity", "project_subtypes: godot",
                   "- ue", "- unity", "- godot")
    )
    if not has_fragile_subtype:
        return False, "coordinator.local.md project_subtypes does not declare ue/unity/godot"

    # Check if session touched any fragile file types
    touched = _session_touched_paths(worktree_root, sid)
    fragile_touched = [
        p for p in touched
        if Path(p).suffix.lower() in _DOC_FRAGILE_EXTENSIONS
    ]
    if not fragile_touched:
        return False, "project_subtypes declared but no fragile file types touched this session"

    return True, (
        f"project_subtypes declares fragile domain; "
        f"fragile files touched: {fragile_touched[:3]}"
    )


# ---------------------------------------------------------------------------
# Plan-claim guard check
# ---------------------------------------------------------------------------


def _check_plan_claim(worktree_root: Path, governing_plan_slug: str) -> tuple[str, str]:
    """Return (verdict, detail) for the plan-claim guard.

    Checks for an atomic-mkdir claim at .git/coordinator-sessions/plan-claims/<slug>/.
    Returns:
      ("unclaimed", detail) — no live claim exists
      ("claimed-self", detail) — claim exists (may be this session's own)
      ("claimed-other", detail) — claim exists from another session

    NOTE: wsc_resolve does NOT attempt to acquire the claim — that is done by
    wsc_commit Step 2.4-a.  Here we only read the claim state for evidence.
    """
    if not governing_plan_slug:
        return "no-slug", "governing plan slug not yet resolved (J-node not answered)"

    # Plan claims live alongside coordinator-sessions in the git common dir
    common_dir = worktree_root / ".git"
    claim_dir = common_dir / "coordinator-sessions" / "plan-claims" / governing_plan_slug
    if not claim_dir.exists():
        return "unclaimed", f"no claim at {rel_id(claim_dir, worktree_root)}"

    # Try to read the holder file
    holder_file = claim_dir / "holder-session-id"
    holder = ""
    if holder_file.exists():
        try:
            holder = holder_file.read_text(encoding="utf-8").strip()
        except OSError:
            print(f"skip: _check_plan_claim: holder = holder_file.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass

    if holder:
        return "claimed-other", f"plan claim held by session {holder}"
    return "claimed-self", "plan claim exists (no holder-session-id readable)"


# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


def _check_idempotency(
    worktree_root: Path, governing_plan_slug: str
) -> tuple[bool, str, list[str]]:
    """Return (fired, detail, scan_errors) for the idempotency guard (Step 2.6.3a).

    Scans archive/completed/**/*.md for a completion entry whose frontmatter
    chain field matches governing_plan_slug.  Returns (True, path, scan_errors)
    when a prior entry exists (ceremony should be a no-op).

    scan_errors carries one entry per unreadable subdir the scan could not
    walk (empty when the scan completed cleanly). A non-empty scan_errors
    means "no prior completion entry found" is NOT a confirmed negative — a
    matching entry could be sitting under the unreadable subtree, which would
    otherwise let ceremony silently re-complete an already-recorded session.
    Callers MUST fence any behaviour change on this signal — see the Tier 2
    marker at the call site.

    Uses os.walk(onerror=...), NOT ``.rglob("*.md")`` — Path.rglob's selector
    silently swallows PermissionError while walking (verified: an unreadable
    subtree yields nothing under it, no exception raised), which previously
    made an already-recorded completion entry under an unreadable subtree
    indistinguishable from "no prior entry exists here".
    """
    if not governing_plan_slug:
        return False, "governing plan slug not yet resolved", []

    archive_dir = worktree_root / "archive" / "completed"
    if not archive_dir.is_dir():
        return False, "archive/completed/ absent — no prior entries", []

    scan_errors: list[str] = []
    walk_errors: list[OSError] = []
    for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=walk_errors.append):
        for fn in sorted(filenames):
            if not fn.endswith(".md"):
                continue
            entry = Path(dirpath) / fn
            try:
                text = entry.read_text(encoding="utf-8")
            except OSError:
                print(f"skip: _check_idempotency: text = entry.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            fm = _parse_frontmatter(text)
            if fm.get("chain", "").strip().strip('"') == governing_plan_slug:
                return True, f"prior completion entry found: {rel_id(entry, worktree_root)}", scan_errors

    if walk_errors:
        for exc in walk_errors:
            log.warning(
                "wsc_resolve: _check_idempotency: cannot scan %s — %s; "
                "subtree dropped (a prior completion entry under it would be "
                "invisible to this scan)",
                getattr(exc, "filename", archive_dir), exc,
            )
            scan_errors.append(f"{getattr(exc, 'filename', archive_dir)}: {exc}")
        # Review: code-reviewer Finding 4 — name the unreadable path(s) directly
        # in the detail string so an operator hitting this doesn't have to
        # reverse-engineer the cause from scan_errors alone (the "scan
        # incomplete" prefix is preserved for existing consumers matching on
        # it; this only appends actionable remediation guidance).
        unreadable = ", ".join(sorted({e.split(":", 1)[0] for e in scan_errors}))
        return (
            False,
            "scan incomplete — cannot assert no prior completion entry — "
            f"idempotency guard degraded, fix filesystem permissions on: {unreadable}",
            scan_errors,
        )

    return False, "no prior completion entry found", scan_errors


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


def _resolve_branches(  # noqa: C901  (complex but linear — each branch is 1 section)
    worktree_root: Path,
    common_dir: Path,
    sid: str,
    session_shape: dict[str, Any],
    shape_source: str,
) -> PipelineContext:
    """Resolve all branch signals and populate a PipelineContext.

    This is the core read-mostly engine.  Each branch in the Branch Inventory
    corresponds to one section below.  The sections are ordered by dependency:
    WSC_DISPOSITION must resolve first (single-session vs chain-terminal gates
    several subsequent branches).

    Returns a fully-populated PipelineContext ready for phase-1 receipt emit.
    """
    # Derive scope_mode from session-shape.json (best-effort)
    scope_mode = ""
    if shape_source == "session_shape":
        plan_block = session_shape.get("plan") or {}
        scope_mode = str(plan_block.get("scope_mode") or "")

    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode=scope_mode,
    )
    ctx.sid = sid

    # -----------------------------------------------------------------------
    # Branch 1 — WSC_DISPOSITION (D)
    # -----------------------------------------------------------------------
    # Determine single-session vs chain-terminal.
    # Primary: session-shape.json pickup.happened field.
    # Fallback (L1b): grep for consumed_by: <sid> in state/handoffs/ when
    #   session-shape.json absent OR pickup field absent.
    consumed_handoff_path = ""
    consumed_handoff_fm: dict[str, Any] = {}
    consumed_handoffs_all: list[tuple[str, dict[str, Any]]] = []
    # Accumulates one entry per unreadable handoffs subtree encountered by any
    # _find_all_consumed_handoffs / _find_consumed_handoff call below (either
    # branch).  A non-empty accumulator means "no consumed_by == sid match
    # found" is NOT a confirmed negative — see the two revert-to-single-session
    # guards below, both fenced Tier 2.
    consumed_handoff_scan_errors: list[str] = []

    env_override = resolve_env_override()
    is_env_override = env_override.escalate

    if is_env_override:
        # ESCALATE-ONLY ENV OVERRIDE (coordinator_core.ops.ceremony.wsc_disposition
        # .resolve_env_override): a positively-detected WSC_DISPOSITION wins
        # outright, ahead of BOTH the session-shape.json primary path and the L1b
        # grep fallback below -- an operator whose session-shape.json says
        # single-session is exactly the operator who needs this escape hatch.
        # Refused-downgrade / unrecognised-value / handoff-alone diagnostics
        # (env_override.diagnostics, non-escalate case) are folded into
        # step0_evidence AFTER the detector chain below runs normally instead --
        # see the env_override_diagnostics assignment just ahead of the
        # Defense-in-depth final gate.
        override_diagnostics = list(env_override.diagnostics)
        consumed_handoff_path = normalize_override_handoff(
            worktree_root, env_override.consumed_handoff_raw, override_diagnostics
        )
        consumed_handoff_fm = {}
        if consumed_handoff_path:
            hf_in_repo = _resolve_in_repo(worktree_root, consumed_handoff_path)
            if hf_in_repo is not None and hf_in_repo.exists():
                try:
                    consumed_handoff_fm = _parse_frontmatter(hf_in_repo.read_text(encoding="utf-8"))
                except OSError:
                    print(f"skip: _resolve_branches: env-override consumed_handoff_fm read failed: {sys.exc_info()[1]}", file=sys.stderr)
            consumed_handoffs_all = [(consumed_handoff_path, consumed_handoff_fm)]
        disposition = WRITE_TOKEN
        disposition_source = "env_override"
        # Referenced unconditionally below (STEP_0 resolving_op) -- the
        # override bypasses both the primary session-shape.pickup path and
        # the L1b grep fallback, so neither label applies; False keeps the
        # resolving_op string on the session_shape branch, which is harmless
        # since disposition_source ("env_override") is what step0_evidence's
        # "method" actually names.
        use_grep_fallback = False
        step0_evidence = {
            "method": disposition_source,
            "disposition": disposition,
            "consumed_handoff_path": consumed_handoff_path,
            "shape_source": shape_source,
            "env_override_diagnostics": override_diagnostics,
        }
    else:
        pickup_field = session_shape.get("pickup")
        use_grep_fallback = (shape_source == "absent") or (pickup_field is None)

        if use_grep_fallback:
            # L1b: disposition resolution via the anchored _find_all_consumed_handoffs
            # match set — grep_paths, when non-empty, already carries the
            # frontmatter-anchored paths (see _grep_disposition's Review comment /
            # F1, F6), so their frontmatter is read directly here rather than
            # re-scanning.
            disposition, grep_paths, grep_scan_errors = _grep_disposition(worktree_root, sid)
            consumed_handoff_scan_errors.extend(grep_scan_errors)
            disposition_source = "grep_fallback"

            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            # Review: code-reviewer Finding 1 — mirrors the primary session-shape
            # path's conservative-fire fencing (:1640-1647, :1704-1715), which
            # this L1b path lacked: a non-empty grep_scan_errors means
            # "single-session" is NOT a confirmed negative — a real predecessor
            # handoff could be sitting under a subtree _find_all_consumed_handoffs
            # could not walk. Force chain-terminal rather than letting an
            # incomplete L1b scan read as a clean single-session close (the exact
            # hazard this function's own docstring names at :427-431). The
            # downstream merged-set reconciliation (:1698-1715) already treats a
            # non-empty consumed_handoff_scan_errors as grounds to suppress any
            # revert-to-single-session, so forcing disposition here and leaving
            # consumed_handoffs_all empty (no path was actually found) resolves
            # consistently through that shared machinery.
            disposition_forced_chain_terminal_scan_incomplete = False
            if canonicalize(disposition) == SINGLE_SESSION and grep_scan_errors:
                disposition = WRITE_TOKEN
                disposition_forced_chain_terminal_scan_incomplete = True
            # --- end Tier 2 ---

            grep_evidence = {
                "method": "anchored_frontmatter_scan",
                "pattern": f"consumed_by: {sid}",
                "paths_found": grep_paths,
                "l1b_reason": "session-shape.json absent or pickup field absent — L1b fallback",
                # Non-empty means the L1b scan could not walk every handoffs
                # subtree — a "single-session" disposition alongside a non-empty
                # scan_errors is NOT a confirmed negative (see _grep_disposition
                # docstring); readers of this receipt must not treat it as clean.
                "scan_errors": grep_scan_errors,
            }
            if grep_paths:
                consumed_handoff_path = grep_paths[0]
                # Review: code-reviewer F4 — no _resolve_in_repo containment guard
                # needed here: consumed_handoff_path is sourced from
                # _find_all_consumed_handoffs's own in-repo glob results (never
                # producer-written JSON), so it can never be absolute or
                # ../-escaping — unlike the primary session-shape.pickup path below.
                for gp in grep_paths:
                    hf_abs = worktree_root / gp
                    if not hf_abs.exists():
                        continue
                    try:
                        gp_fm = _parse_frontmatter(hf_abs.read_text(encoding="utf-8"))
                    except OSError:
                        print(f"skip: _resolve_branches: gp_fm = _parse_frontmatter(hf_abs.read_text(encoding=\"utf-8\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
                        continue
                    consumed_handoffs_all.append((gp, gp_fm))
                consumed_handoff_fm = consumed_handoffs_all[0][1] if consumed_handoffs_all else {}
            step0_evidence = {
                **grep_evidence,
                "disposition": disposition,
                "shape_source": shape_source,
            }
            if disposition_forced_chain_terminal_scan_incomplete:
                step0_evidence["disposition_forced_chain_terminal_scan_incomplete"] = True
        else:
            # Primary: session-shape.json pickup field
            pickup_happened = bool(pickup_field.get("happened", False)) if isinstance(pickup_field, dict) else False
            disposition = WRITE_TOKEN if pickup_happened else SINGLE_SESSION
            disposition_source = "session_shape"
            consumed_handoff_path = ""
            if pickup_happened and isinstance(pickup_field, dict):
                consumed_handoff_path = str(pickup_field.get("handoff", ""))

            # Resolve consumed handoff frontmatter if chain-terminal
            rejected_handoff_path = ""
            rejected_for_repo_escape = ""
            if canonicalize(disposition) == PREDECESSOR_CONSUMED and consumed_handoff_path:
                # Review: code-reviewer F2 — removed always-false dead block; Python's Path join
                # with an absolute worktree_root always produces an absolute path, so the
                # is_absolute() guard was never True and the inner assignment was unreachable.
                #
                # Repo-containment guard (foreign-repo bleed defect): pickup.handoff
                # is producer-written and NOT trusted — if it is absolute or contains ..,
                # Path.__truediv__ lets it escape worktree_root entirely, pointing at a
                # file in ANOTHER repo (e.g. Example-retrieval-repo).  _resolve_in_repo asserts the
                # candidate resolves to a location INSIDE worktree_root before we even
                # check existence; a bare hf_abs.exists() check alone (the pre-fix shape)
                # would happily validate a foreign file that also happens to satisfy
                # consumed_by == sid, threading a foreign consumed_handoff/predecessor
                # downstream into STEP_2_7 and leaving the session's real handoff frozen
                # at in_flight.  Negative-spec: do NOT drop this containment check even
                # though the consumed_by == sid check below looks sufficient — existence
                # + consumed_by alone do not assert the file lives in THIS repo.
                hf_in_repo = _resolve_in_repo(worktree_root, consumed_handoff_path)

                # Concurrency-correctness guard: pickup.handoff in session-shape.json
                # is producer-written and can point at a temporally-adjacent CONCURRENT
                # session's handoff (a peer's), not this session's — session-shape.json
                # does not itself assert ownership.  Verify consumed_by == sid via the
                # anchored helper before trusting the path; otherwise a peer's in-flight
                # handoff gets misattributed as this session's consumed predecessor.
                # (Originating incident: DoE-claude 2026-07-09 — a /workstream-complete
                # nearly stamped a live peer workstream's handoff as shipped.)
                if (
                    hf_in_repo is not None
                    and hf_in_repo.exists()
                    and _get_handoff_consumed_by(str(hf_in_repo)) == sid
                ):
                    try:
                        consumed_handoff_fm = _parse_frontmatter(
                            hf_in_repo.read_text(encoding="utf-8")
                        )
                    except OSError:
                        print(f"skip: _resolve_branches: consumed_handoff_fm = _parse_frontmatter( failed: {sys.exc_info()[1]}", file=sys.stderr)
                        pass
                else:
                    if hf_in_repo is None:
                        rejected_for_repo_escape = consumed_handoff_path
                    rejected_handoff_path = consumed_handoff_path
                    consumed_handoff_path = ""

            # If chain-terminal but no path in session-shape (including a path that
            # was discarded above for failing the consumed_by == sid check), the
            # anchored recovery scan just below (the Staff Engineer F0) re-derives every
            # sid-owned handoff via _find_all_consumed_handoffs — consumed_handoff_path/
            # consumed_handoff_fm are backfilled from that same scan's result rather
            # than a separate _find_consumed_handoff call.
            # Review: code-reviewer Finding 3 — previously called _find_consumed_handoff
            # here (itself a thin wrapper over _find_all_consumed_handoffs), then
            # unconditionally re-ran _find_all_consumed_handoffs again a few lines
            # below for the SAME disposition branch — doubling the handoffs/ walk
            # cost and duplicating any scan_errors entry once per walk. Deriving
            # from scan_group[0] below removes the redundant first walk entirely.

            # the Staff Engineer F0: a DAG pickup can own N predecessor handoffs even when
            # session-shape.json's pickup field only names one. UNION the
            # pickup-path result (already per-element verified above: repo-
            # containment + consumed_by == sid) with the archive-aware scan for
            # ALL sid-owned handoffs. The two sources are NOT disjoint — the scan
            # will re-find the same handoff the pickup path already resolved — so
            # merge via dedup-by-normalized-relpath, pickup-path result FIRST so
            # consumed_handoffs[0] preserves today's scalar-fallback ordering.
            if canonicalize(disposition) == PREDECESSOR_CONSUMED:
                pickup_group: list[tuple[str, dict[str, Any]]] = (
                    [(consumed_handoff_path, consumed_handoff_fm)] if consumed_handoff_path else []
                )
                scan_group = _find_all_consumed_handoffs(
                    worktree_root, sid, scan_errors=consumed_handoff_scan_errors
                )
                consumed_handoffs_all = _dedup_handoffs_by_relpath(pickup_group, scan_group)
                if not consumed_handoff_path and consumed_handoffs_all:
                    consumed_handoff_path, consumed_handoff_fm = consumed_handoffs_all[0]

            # Concurrency-correctness revert: session_shape.pickup.happened only
            # hypothesizes chain-terminal — it is producer-written and, on a shared
            # concurrent-EM work/* branch, can point at a peer's handoff (rejected
            # above) with no sid-owned predecessor anywhere on disk (the recovery
            # scan above also came up empty). Without this revert the disposition
            # stays stuck at "chain-terminal" with an empty consumed_handoff_path,
            # which produces a spurious wsc_commit STEP_2_7 "evidence gap" op_tail
            # failure on what is really a single-session close. Absent a
            # consumed_by == sid match from either source, this session owns no
            # consumed predecessor — revert to single-session.
            disposition_reverted_to_single_session = False
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            # A non-empty consumed_handoff_scan_errors means "no consumed_by ==
            # sid match found" is NOT a confirmed negative — an unreadable
            # handoffs subtree could be hiding this session's real predecessor.
            # Do NOT silently revert to single-session on an incomplete scan;
            # leave disposition at chain-terminal so the pre-existing fail-loud
            # STEP_2_7 "evidence gap" path in wsc_commit.py surfaces the problem,
            # instead of a clean-looking but potentially wrong single-session close.
            if (
                canonicalize(disposition) == PREDECESSOR_CONSUMED
                and not consumed_handoff_path
                and not consumed_handoff_scan_errors
            ):
                disposition = SINGLE_SESSION
                disposition_reverted_to_single_session = True
            # --- end Tier 2 ---

            step0_evidence = {
                "method": disposition_source,
                "pickup_happened": pickup_happened if not use_grep_fallback else None,
                "disposition": disposition,
                "consumed_handoff_path": consumed_handoff_path,
                "shape_source": shape_source,
                "scan_errors": list(consumed_handoff_scan_errors),
            }
            if rejected_handoff_path:
                step0_evidence["session_shape_handoff_rejected"] = rejected_handoff_path
            if rejected_for_repo_escape:
                # Distinguishable in the receipt from the generic-rejection case above
                # (peer misattribution / missing file / consumed_by mismatch) — this
                # one specifically means the producer-written path escaped worktree_root
                # (absolute path or ../ traversal), the foreign-repo bleed defect.
                step0_evidence["consumed_handoff_outside_repo"] = rejected_for_repo_escape
            if disposition_reverted_to_single_session:
                step0_evidence["disposition_reverted_to_single_session"] = True

    if (not is_env_override) and env_override.diagnostics:
        # Refused single-session downgrade, unrecognised value, or
        # WSC_CONSUMED_HANDOFF-without-WSC_DISPOSITION -- the detector chain above
        # ran exactly as if no override were present; this just makes the
        # non-effect visible in the receipt. (The escalate case already set
        # env_override_diagnostics inside its own step0_evidence, above.)
        step0_evidence["env_override_diagnostics"] = list(env_override.diagnostics)


    # Defense-in-depth final gate (foreign-repo bleed defect, 2026-07-13 cockpit
    # incident): re-verify containment + sid-ownership on the MERGED set at the one
    # point every source (session-shape pickup, archive-aware UNION, grep fallback)
    # converges, before it flows into the receipt and STEP_2_7's stamp target.  Any
    # foreign/absolute/../-escaping path or peer-owned handoff that slipped past a
    # per-source guard is dropped here and re-expressed as a repo-relative path, so
    # neither the absolute foreign path nor a relativized phantom can survive.
    operator_asserted_paths: list[str] = []
    consumed_handoffs_all, sanitized_out = _sanitize_consumed_handoffs(
        worktree_root, sid, consumed_handoffs_all,
        operator_asserted=is_env_override,
        operator_asserted_paths=operator_asserted_paths,
    )
    consumed_handoff_paths_all = [path for path, _fm in consumed_handoffs_all]
    step0_evidence["consumed_handoff_paths"] = consumed_handoff_paths_all
    if "consumed_handoff_path" in step0_evidence:
        # Keep the receipt scalar consistent with the sanitized plural (session_shape
        # branch seeds this key with the pre-sanitization value).
        step0_evidence["consumed_handoff_path"] = (
            consumed_handoff_paths_all[0] if consumed_handoff_paths_all else ""
        )
    if sanitized_out:
        # Distinguishable in the receipt: a path reached the merged set but failed
        # the final containment/ownership gate (belt-and-suspenders over the
        # per-source guards above).
        step0_evidence["consumed_handoff_paths_rejected"] = sanitized_out
    if operator_asserted_paths:
        # ESCALATE-ONLY ENV OVERRIDE (operator_asserted=True, env-override
        # callers only): these entries were kept WITHOUT a matching
        # consumed_by == sid stamp -- the operator asserted ownership
        # manually via WSC_CONSUMED_HANDOFF (the exact case for a dead
        # claiming session or a ship-then-archive handoff with no live
        # consume stamp). Record it loudly so a receipt reader can never
        # mistake this for a normally-verified path.
        step0_evidence["consumed_handoff_ownership_operator_asserted"] = True
        override_notes = step0_evidence.setdefault("env_override_diagnostics", [])
        for asserted_path in operator_asserted_paths:
            override_notes.append(
                f"NOTE: consumed_handoff={asserted_path} accepted on operator "
                f"assertion — WSC_DISPOSITION override bypassed the "
                f"consumed_by == {sid!r} ownership check (containment and "
                "existence still enforced); this file's own frontmatter does "
                "not stamp this session as its consumer."
            )

    # Reconcile the scalar with the sanitized plural: consumed_handoff_path /
    # consumed_handoff_fm feed ctx.consumed_handoff / ctx.predecessor and must not
    # retain a value the final gate dropped.  When the sanitized set is empty the
    # session owns no valid consumed predecessor — revert chain-terminal→single-session
    # exactly as the per-source revert above intends (a foreign-only pickup that
    # slipped the scalar guard must not leave disposition stuck at chain-terminal).
    if consumed_handoffs_all:
        if consumed_handoff_path not in consumed_handoff_paths_all:
            consumed_handoff_path, consumed_handoff_fm = consumed_handoffs_all[0]
    elif is_env_override:
        # ESCALATE-ONLY ENV OVERRIDE: the override already trusted
        # disposition=predecessor-consumed ahead of the whole detector chain
        # (see resolve_env_override / the Branch-1 override block above). An
        # empty consumed_handoffs_all here just means no WSC_CONSUMED_HANDOFF
        # was given (or it failed the containment/ownership gate above) --
        # NOT a negative signal. Consulting Detector B or reverting to
        # single-session below would silently undo the override's escalation,
        # exactly the downgrade-by-detector-chain the override exists to
        # bypass -- so this path is deliberately excluded from Tier 2 below.
        consumed_handoff_path = ""
        consumed_handoff_fm = {}
    else:
        consumed_handoff_path = ""
        consumed_handoff_fm = {}
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # Detector B (git-provenance, cross-repo/inbox/2026-07-22-claude-central-em-
        # wsc-tail-cutover-contract.md Ask 2): every filesystem-scan source above
        # (live consumed_by stamp, archive scan, grep fallback) came up empty for
        # sid AND the scan itself completed cleanly (no consumed_handoff_scan_errors)
        # — before concluding single-session, consult git's OWN provenance for a
        # ship-then-archive shape none of those scans can see (a session that
        # archived a predecessor handoff this run with no live consumed_by stamp
        # ever written). See resolver.detect_git_provenance_consumed's docstring
        # for the full algorithm (ported from the bash oracle's Detector B) and its
        # two hit-guards (well-formedness, restoration-commit spoof).
        detector_b_warnings: list[str] = []
        if not consumed_handoff_scan_errors:
            b_hits, detector_b_warnings = detect_git_provenance_consumed(worktree_root, sid)
            if b_hits:
                consumed_handoffs_all, sanitized_out_b = _sanitize_detector_b_hits(
                    worktree_root, b_hits
                )
                if consumed_handoffs_all:
                    consumed_handoff_path, consumed_handoff_fm = consumed_handoffs_all[0]
                    consumed_handoff_paths_all = [p for p, _fm in consumed_handoffs_all]
                if sanitized_out_b:
                    step0_evidence["consumed_handoff_paths_rejected"] = list(
                        step0_evidence.get("consumed_handoff_paths_rejected") or []
                    ) + sanitized_out_b
        if detector_b_warnings:
            # Fail-loud WARN (never a silent drop) — surfaced whether or not a
            # hit was ultimately accepted, so a rejected/guarded candidate is
            # visible in the receipt even when disposition stays single-session.
            step0_evidence["detector_b_warnings"] = detector_b_warnings

        if consumed_handoffs_all:
            disposition = WRITE_TOKEN
            step0_evidence["disposition"] = disposition
            step0_evidence["disposition_source_detector_b"] = True
            step0_evidence["consumed_handoff_paths"] = consumed_handoff_paths_all
            step0_evidence["consumed_handoff_path"] = consumed_handoff_path
        # Same guard as the per-source revert above: an empty merged set
        # caused by an incomplete scan (consumed_handoff_scan_errors non-empty)
        # is NOT the same as a foreign-only pickup that legitimately sanitized
        # to nothing — do not revert to single-session on an incomplete scan.
        elif canonicalize(disposition) == PREDECESSOR_CONSUMED and not consumed_handoff_scan_errors:
            disposition = SINGLE_SESSION
            step0_evidence["disposition"] = disposition
            step0_evidence["disposition_reverted_to_single_session"] = True
        elif canonicalize(disposition) == PREDECESSOR_CONSUMED:
            step0_evidence["disposition_revert_suppressed_scan_incomplete"] = True
        # --- end Tier 2 ---

    ctx.disposition = disposition
    if canonicalize(disposition) == PREDECESSOR_CONSUMED:
        ctx.consumed_handoff = consumed_handoff_path
        ctx.predecessor = consumed_handoff_fm.get("predecessor", "")
        ctx.consumed_handoffs = consumed_handoff_paths_all
        ctx.predecessors = [fm.get("predecessor", "") for _path, fm in consumed_handoffs_all]

    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_WSC_DISPOSITION,
        legible=True,
        node_type="D",
        signal_read=disposition,
        evidence=step0_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_0,
        resolving_op=(
            "env:WSC_DISPOSITION" if is_env_override
            else "grep:consumed_by+session_shape.pickup" if use_grep_fallback
            else "session_shape.pickup"
        ),
        evidence=step0_evidence,
    ))

    # -----------------------------------------------------------------------
    # Branch 2 — Governing plan exists (J — which plan opened requires session memory)
    # -----------------------------------------------------------------------
    candidate_plans = _list_candidate_plans(worktree_root, sid)
    governing_plan_evidence = {
        "method": "docs/plans/ glob",
        "candidate_count": len(candidate_plans),
        "candidate_plans": candidate_plans[:20],  # cap for receipt size
        "note": (
            "File existence is D-readable; which plan was opened this session "
            "requires EM session memory — J-node follows."
        ),
    }
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_GOVERNING_PLAN,
        legible=False,  # J — requires EM session memory for the specific plan
        node_type="J",
        signal_read=candidate_plans,
        evidence={**governing_plan_evidence, "question": "Which plan doc (if any) was the subject of this session?"},
    ))
    # step_2a in the node-map is D (SHOULD-BE-SCRIPT: scan docs/plans/ for candidates).
    # The branch-level "governing plan exists" is J (which plan was opened requires EM
    # session memory), captured above in the BranchResolution.  The step node is D only.
    ctx.add_node(handle_d(
        STEP_2A,
        resolving_op="glob:docs/plans/*.md",
        evidence=governing_plan_evidence,
    ))

    # -----------------------------------------------------------------------
    # Branch 3 — Chain slug 4-way (D for all cases a/b/c/d)
    # -----------------------------------------------------------------------
    # Case a (plan-added this session): git log --diff-filter=A --since=started_at — D
    # Case b (handoff picked up): consumed_handoff_path frontmatter chain field — D
    # Case c (slug in handoff frontmatter): consumed_handoff_fm.chain field — D
    # Case d (null/fallback): always available — D
    chain_slug = ""
    if consumed_handoff_fm:
        chain_slug = consumed_handoff_fm.get("chain", "").strip().strip('"')

    # Case a: read started_at + added-plan predicate (C1)
    started_at = _read_started_at(common_dir, sid)
    added_plans: list[str] = []
    if started_at:
        added_plans = _session_added_plans(worktree_root, sid, started_at)

    case_a_signal = (
        f"plans added this session: {added_plans}"
        if started_at
        else "started_at absent — negative signal (no plans attributable)"
    )

    chain_slug_evidence = {
        "case_a_plan_added": case_a_signal,
        "case_a_added_plans": added_plans,
        "case_b_consumed_handoff": consumed_handoff_path,
        "case_c_chain_from_frontmatter": chain_slug,
        "case_d_fallback": "null-slug available if no other case resolves",
        "resolved_slug": chain_slug or "",
    }
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_CHAIN_SLUG,
        legible=True,
        node_type="D",
        signal_read=chain_slug or None,
        evidence=chain_slug_evidence,
    ))
    # D node for chain-slug case-a: plan-added predicate via started_at + git log
    ctx.add_node(handle_d(
        STEP_2_6_3,
        resolving_op="git log --diff-filter=A --since=started_at -- docs/plans/*.md",
        evidence={
            "started_at": started_at,
            "added_plans": added_plans,
            "signal": case_a_signal,
        },
    ))

    # -----------------------------------------------------------------------
    # Branch 4 — Idempotency guard (D)
    # -----------------------------------------------------------------------
    idempotency_fired_raw, idempotency_detail, idempotency_scan_errors = _check_idempotency(
        worktree_root, chain_slug
    )
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # An unreadable archive/completed subtree must not present as "no prior
    # completion entry" — a matching entry could be sitting under it. Force
    # the guard to read as fired (conservative no-op) when the scan could not
    # fully complete, so ceremony does not silently re-complete an
    # already-recorded session on an incomplete scan.  scan_errors and detail
    # keep the raw distinction visible for receipt readers.
    idempotency_fired = idempotency_fired_raw or bool(idempotency_scan_errors)
    # --- end Tier 2 ---
    idempotency_evidence = {
        "method": "os.walk:archive/completed/**/*.md chain-field scan",
        "governing_plan_slug": chain_slug,
        "fired": idempotency_fired,
        "fired_raw_match": idempotency_fired_raw,
        "detail": idempotency_detail,
        "scan_errors": idempotency_scan_errors,
    }
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_IDEMPOTENCY_GUARD,
        legible=True,
        node_type="D",
        signal_read=idempotency_fired,
        evidence=idempotency_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_2_6_3A,
        resolving_op="os.walk:archive/completed/**/*.md",
        evidence=idempotency_evidence,
    ))

    # -----------------------------------------------------------------------
    # Branch 5 — Nature classification (D)
    # -----------------------------------------------------------------------
    commit_msgs = _session_commit_log(worktree_root, sid)
    touched_paths = _session_touched_paths(worktree_root, sid)
    nature = classify_nature(touched_paths, commit_msgs)
    nature_evidence = {
        "method": "path_pattern+commit_keyword_heuristic",
        "commit_count": len(commit_msgs),
        "path_count": len(touched_paths),
        "nature": nature,
        # Review: code-reviewer F3 — replaced __import__("os") with module-level import os
        "override_from_env": bool(os.environ.get("COMPLETION_NATURE", "")),
    }
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_NATURE_CLASSIFICATION,
        legible=True,
        node_type="D",
        signal_read=nature,
        evidence=nature_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_2_6_4,
        resolving_op="completion_nature.classify_nature",
        evidence=nature_evidence,
    ))

    # -----------------------------------------------------------------------
    # Branch 6 — Lesson qualifies (J)
    # -----------------------------------------------------------------------
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_LESSON_QUALIFIES,
        legible=False,
        node_type="J",
        signal_read=None,
        evidence={
            "note": "EM session memory required — no disk signal for lesson qualification",
        },
    ))
    ctx.add_node(emit_j(STEP_1A, answer=""))

    # -----------------------------------------------------------------------
    # Branch 7 — Lesson is universal (J)
    # -----------------------------------------------------------------------
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_LESSON_UNIVERSAL,
        legible=False,
        node_type="J",
        signal_read=None,
        evidence={
            "note": "EM judgment about lesson scope — no disk signal",
        },
    ))
    ctx.add_node(emit_j(STEP_1_2, answer=""))

    # -----------------------------------------------------------------------
    # Branch 8 — Review wave scale / B1 dispatch-plan (D)
    # -----------------------------------------------------------------------
    # Pre-resolve the B1 dispatch-plan:
    #   - diff LOC from session commits
    #   - commit count for the session
    #   - surface count from touched paths
    #   - brightline verdict: PARTITION-MANDATORY vs single-reviewer-ok
    # The review wave execution + adjudication is EM-owned (B bracket).
    #
    # C3 — consume the C1 analyze_session_scoping() verdict before trusting
    # the trailer-grep diff_loc/commit_count below.  known_scope_paths unions
    # this sid's own trailer-scoped touched_paths (already resolved above,
    # Branch 5) with its consumed-handoff/plan-added paths — the same scope
    # union _detect_foreign_commits documents as "supplied by the caller".
    known_scope_paths = frozenset(touched_paths) | frozenset(
        path for path, _fm in consumed_handoffs_all
    )
    scoping_verdict = analyze_session_scoping(
        worktree_root, common_dir, sid, known_scope_paths=known_scope_paths,
    )
    ctx.scoping_method = scoping_verdict.method
    ctx.foreign_commit_count = scoping_verdict.foreign_count

    if scoping_verdict.method == SCOPING_METHOD_STARTED_AT_RANGE:
        # Trailerless-but-recoverable: the started_at contiguous range IS the
        # recovered scope — recorded as sha_range for receipt-level
        # auditability.  diff_loc/commit_count/surface_count below are
        # recomputed OVER candidate_range (git rev-list/log --stat/--name-only)
        # rather than via the trailer-grep helpers: this branch is reached
        # precisely when _trailer_reliable() is False — the trailer grep is
        # the exact unreliable signal that triggered this branch, and would
        # silently report diff_loc:0/commit_count:0 for a large trailerless
        # session, re-opening AC3's "silent empty scope" failure one level up
        # at the B-wave brightline gate.  See Finding 3,
        # docs/plans/2026-07-12-wsc-concurrent-tree-safety-hardening.md.
        sha_range = scoping_verdict.candidate_range
        # scoping_git_warnings accumulates every _git_run TIMEOUT hit while
        # deriving this branch's numbers — scoping_verdict.warnings covers the
        # _trailer_reliable/_started_at_candidate_range reads that selected
        # this branch; the three _range_* calls below add their own (Review:
        # code-reviewer F1). Folded into the branch/B1 evidence below so a
        # degraded diff_loc/commit_count/surface_count is never silently
        # indistinguishable from a genuinely-computed one.
        scoping_git_warnings = list(scoping_verdict.warnings)
        diff_loc_raw = _range_diff_loc(worktree_root, sha_range, warnings=scoping_git_warnings)
        commit_count_raw = _range_commit_count(worktree_root, sha_range, warnings=scoping_git_warnings)
        touched_raw = _range_touched_paths(worktree_root, sha_range, warnings=scoping_git_warnings)
        # Each of the three probes above returns `None` — NOT 0/[] — specifically when its
        # `git` call TIMED OUT (see each function's own docstring); a `None` clears no
        # threshold, it forces `partition_mandatory` directly, below. This is the single
        # source of truth for "this range was unmeasurable" — no separate warnings-count
        # heuristic. Every threshold comparison below reads the COALESCED value, never the
        # raw one, so a `None` can never reach a `>=` or a JSON payload.
        range_probe_indeterminate = (
            diff_loc_raw is None or commit_count_raw is None or touched_raw is None
        )
        diff_loc = diff_loc_raw if diff_loc_raw is not None else 0
        commit_count = commit_count_raw if commit_count_raw is not None else 0
        surface_count = _session_surface_count(touched_raw if touched_raw is not None else [])
    else:
        scoping_git_warnings = list(scoping_verdict.warnings)
        # SCOPING_METHOD_TRAILER — trailer scoping is reliable here; existing
        # grep helpers stay authoritative.
        # SCOPING_METHOD_AMBIGUOUS — sha_range is moot; the X-node path below
        # discards diff_loc/commit_count/surface_count entirely rather than
        # trusting a (possibly foreign-contaminated) grep/range result.
        sha_range = ""
        diff_loc = _session_diff_loc(worktree_root, sid)
        # Preexisting fail-open behaviour at this call site is left unchanged by the C2
        # sub-task: a degraded read is folded into commit_count=0 here, matching the old
        # bare-int `_session_commit_count`. The degraded-with-evidence distinction is
        # surfaced by the facade (coordinator_core/session/session_facts.py), not
        # retrofitted onto this pre-existing brightline branch.
        commit_count_record = session_commit_count_attributed(worktree_root, sid)
        commit_count = (
            commit_count_record["value"] if not commit_count_record["degraded"] else 0
        )
        surface_count = _session_surface_count(touched_paths)
        # Same inversion as the range branch above: a degraded read folds to 0, and 0
        # argues for LESS review. Included here too because the rule is about the
        # decision's safe direction, not about which branch produced the number.
        range_probe_indeterminate = bool(commit_count_record["degraded"])

    # An input we could not measure must never argue for less review. `0` is a measured
    # value meaning "small"; "we could not check" is a different fact with the opposite
    # safety consequence, and collapsing the two is how a large change slips through as
    # single-reviewer-ok. Indeterminate therefore forces the mandate rather than clearing
    # it — over-reviewing a small change costs a reviewer, under-reviewing a large one is
    # what the brightline exists to prevent.
    partition_mandatory = (
        range_probe_indeterminate
        or diff_loc >= _BRIGHTLINE_LOC
        or commit_count >= _BRIGHTLINE_COMMITS
        or surface_count >= _BRIGHTLINE_SURFACES
    )
    brightline_verdict = "PARTITION-MANDATORY" if partition_mandatory else "single-reviewer-ok"

    # Doc-fragile check (for docs-checker inclusion in dispatch-plan)
    doc_fragile_active, doc_fragile_reason = _check_doc_fragile_domain(worktree_root, sid)

    if scoping_verdict.method == SCOPING_METHOD_AMBIGUOUS:
        # Ambiguous/interleaved scoping — do NOT thread the (possibly
        # foreign-contaminated) trailer-grep/candidate-range numbers into the
        # B1 review-wave dispatch plan; a contaminated sha_range/
        # partition_boundaries silently threads a peer session's commits
        # into this session's review wave.  Force an EM-supplied commit set
        # via a graceful-negative X-node, the same make_x_node-direct
        # pattern STEP_2_67A already uses for a D-classified step (emit_x is
        # unusable here — X_MISSING_SIGNALS is empty, node_handlers.py:
        # 245-250 / :394-419 — it would KeyError for every step_id, this one
        # included).
        ctx.add_branch(BranchResolution(
            branch_id=BRANCH_ID_REVIEW_WAVE_SCALE,
            legible=False,
            node_type="X",
            signal_read=None,
            evidence={
                "method": "analyze_session_scoping:ambiguous",
                "scoping_method": scoping_verdict.method,
                "foreign_commit_count": scoping_verdict.foreign_count,
                "contiguous": scoping_verdict.contiguous,
                "candidate_range": scoping_verdict.candidate_range,
                "scoping_git_warnings": scoping_git_warnings,
                "note": (
                    "trailer unreliable and the started_at range is either "
                    "foreign-contaminated or non-contiguous — EM must supply "
                    "the true session commit set before the review wave can "
                    "be scoped; do NOT trust the trailer-grep diff_loc/"
                    "commit_count for this session"
                ) + (
                    " — NOTE: scoping_git_warnings is non-empty, so this "
                    "verdict may itself be a git-timeout fail-open default "
                    "rather than a confirmed ambiguous scope"
                    if scoping_git_warnings else ""
                ),
            },
        ))
        ctx.add_node(make_x_node(
            node_id=STEP_2_9A,
            missing_signal="SESSION_COMMIT_SCOPE",
        ))
        b1_pre_resolved = {
            "brightline_verdict": "AMBIGUOUS-SCOPE",
            "scoping_method": scoping_verdict.method,
            "foreign_commit_count": scoping_verdict.foreign_count,
            "diff_loc": None,
            "commit_count": None,
            "surface_count": None,
            "slice_count": None,
            "sha_range": None,
            "partition_boundaries": [],
            "docs_checker_inclusion": False,
            "thresholds": {
                "loc": _BRIGHTLINE_LOC,
                "commits": _BRIGHTLINE_COMMITS,
                "surfaces": _BRIGHTLINE_SURFACES,
            },
        }
        ctx.add_node(emit_b(
            STEP_B1,
            pre_resolved_evidence=b1_pre_resolved,
            em_adjudication={},  # filled during EM turn
        ))
    else:
        b1_pre_resolved = {
            "brightline_verdict": brightline_verdict,
            "scoping_method": scoping_verdict.method,
            "foreign_commit_count": scoping_verdict.foreign_count,
            "diff_loc": diff_loc,
            "commit_count": commit_count,
            "surface_count": surface_count,
            "slice_count": max(1, surface_count) if partition_mandatory else 1,
            "sha_range": sha_range,
            "partition_boundaries": touched_paths[:_BRIGHTLINE_SURFACES] if partition_mandatory else [],
            "docs_checker_inclusion": doc_fragile_active,
            "thresholds": {
                "loc": _BRIGHTLINE_LOC,
                "commits": _BRIGHTLINE_COMMITS,
                "surfaces": _BRIGHTLINE_SURFACES,
            },
        }

        review_wave_evidence = {
            "method": "git_log_stat+session_surface_count+doc_fragile_check",
            "brightline_verdict": brightline_verdict,
            "scoping_method": scoping_verdict.method,
            "foreign_commit_count": scoping_verdict.foreign_count,
            "diff_loc": diff_loc,
            "commit_count": commit_count,
            "surface_count": surface_count,
            "doc_fragile_active": doc_fragile_active,
            "doc_fragile_reason": doc_fragile_reason,
            # Non-empty iff a git read behind diff_loc/commit_count/surface_count
            # above hit its timeout — see scoping_git_warnings' assembly comment
            # above (Review: code-reviewer F1).
            "scoping_git_warnings": scoping_git_warnings,
            "pre_resolved_evidence": b1_pre_resolved,
        }
        ctx.add_branch(BranchResolution(
            branch_id=BRANCH_ID_REVIEW_WAVE_SCALE,
            legible=True,
            node_type="D",
            signal_read=brightline_verdict,
            evidence=review_wave_evidence,
        ))
        # D nodes for brightline sub-steps
        ctx.add_node(handle_d(
            STEP_2_9A,
            resolving_op="git_log_stat:brightline_gate",
            evidence={
                "diff_loc": diff_loc,
                "commit_count": commit_count,
                "surface_count": surface_count,
                "verdict": brightline_verdict,
                "scoping_method": scoping_verdict.method,
            },
        ))
        ctx.add_node(handle_d(
            STEP_2_9B,
            resolving_op="git_log_stat:row_selection",
            evidence={
                "partition_mandatory": partition_mandatory,
                "slice_count": b1_pre_resolved["slice_count"],
                "partition_boundaries": b1_pre_resolved["partition_boundaries"],
            },
        ))
        # B1 — the review wave bracket node
        ctx.add_node(emit_b(
            STEP_B1,
            pre_resolved_evidence=b1_pre_resolved,
            em_adjudication={},  # filled during EM turn
        ))

    # Doc-fragile D node (2.9e feeds B1 dispatch-plan docs-checker inclusion flag)
    # — unconditional: doc-fragile detection does not depend on commit-scoping
    # trust, so it is computed and emitted regardless of scoping_verdict.method.
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_DOC_FRAGILE_DOMAIN,
        legible=True,
        node_type="D",
        signal_read=doc_fragile_active,
        evidence={"active": doc_fragile_active, "reason": doc_fragile_reason},
    ))
    ctx.add_node(handle_d(
        STEP_2_9E,
        resolving_op="coordinator.local.md:project_subtypes+git_diff:filetype",
        evidence={"active": doc_fragile_active, "reason": doc_fragile_reason},
    ))

    # -----------------------------------------------------------------------
    # Branch 9 — Open memos in cross-repo/inbox/ (D)
    # -----------------------------------------------------------------------
    open_memos, open_memos_scan_error = _scan_open_memos(worktree_root)
    open_memos_evidence = {
        "method": "iterdir:cross-repo/inbox/*.md+frontmatter_parse",
        "open_count": len(open_memos),
        "memos": open_memos,
        # Non-None means the inbox could not be enumerated — open_count: 0
        # here is a scan failure, NOT "no open memos" (see Tier 2 below).
        "scan_error": open_memos_scan_error,
    }
    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # An unreadable inbox must not present as "no open memos" — open_count: 0
    # is indistinguishable from a genuinely-clean inbox on its own, so force
    # signal_read True on a scan failure (this D-node does NOT get to proceed
    # clean when the scan itself failed).
    open_memos_signal = len(open_memos) > 0 or open_memos_scan_error is not None
    # --- end Tier 2 ---
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_OPEN_MEMOS,
        legible=True,
        node_type="D",
        signal_read=open_memos_signal,
        evidence=open_memos_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_2_65A,
        resolving_op="iterdir:cross-repo/inbox/*.md",
        evidence=open_memos_evidence,
    ))

    # -----------------------------------------------------------------------
    # Branch 10 — Which memos resolved this session (J)
    # -----------------------------------------------------------------------
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_MEMOS_RESOLVED,
        legible=False,
        node_type="J",
        signal_read=None,
        evidence={
            "open_memos": open_memos,
            "note": (
                "No reliable programmatic signal connects commits to memo resolution. "
                "EM session memory required."
            ),
        },
    ))
    ctx.add_node(emit_j(STEP_2_65B, answer=""))

    # -----------------------------------------------------------------------
    # Branch 11 — Session-authored transient files exist (D or graceful-negative X)
    # -----------------------------------------------------------------------
    # started_at computed above (chain-slug case-a, same function scope).
    # _scan_session_scratch returns None on absent started_at OR scan error → X path.
    scratch_count = _scan_session_scratch(worktree_root, started_at)
    if scratch_count is None:
        # Graceful-negative: started_at absent or scan error — X-shaped no-signal.
        # Deliberate divergence from canonical first-commit fallback (self-clean L56):
        # first-commit is not reliably session start (no-commit / rebase cases).
        ctx.add_branch(BranchResolution(
            branch_id=BRANCH_ID_SESSION_AUTHORED_FILES,
            legible=False,
            node_type="X",
            signal_read=None,
            evidence={
                "missing_signal": "SESSION_START_TIME",
                "note": (
                    "started_at sentinel absent — X-shaped no-signal "
                    "(deliberate divergence from canonical first-commit fallback: "
                    "first-commit is not reliably session start)"
                ),
            },
        ))
        # emit_x requires X_MISSING_SIGNALS registration; STEP_2_67A was removed from
        # that registry by C2 (reclassified D).  Graceful-negative produces the same
        # schema-valid X-node shape via make_x_node directly.
        # Negative-spec: do NOT restore STEP_2_67A to X_MISSING_SIGNALS — it is D.
        ctx.add_node(make_x_node(
            node_id=STEP_2_67A,
            missing_signal="SESSION_START_TIME",
        ))
    else:
        # D path: deterministic filesystem-mtime enumeration.
        ctx.add_branch(BranchResolution(
            branch_id=BRANCH_ID_SESSION_AUTHORED_FILES,
            legible=True,
            node_type="D",
            signal_read=scratch_count,
            evidence={
                "method": "filesystem-mtime-scan",
                "started_at": started_at,
                "scratch_roots": ["tasks/"],
                "applied_exclusions": [
                    "git-tracked",
                    "keep-list:todo.md|plan.md|completion-log.md|*.plan.md",
                    "cross-repo/inbox:deferred-step-2.65-ordering",
                    (
                        "case-b:mitigated-by-untracked+mtime-filter;"
                        "full-sibling-scope-read-deferred"
                    ),
                ],
                "candidate_count": scratch_count,
                "scope_note": (
                    "deterministic subset of canonical enumeration "
                    "(canonical includes non-mechanizable 'EM working notes'); "
                    "STEP_2_67B/J does the full-judgment pass"
                ),
            },
        ))
        ctx.add_node(handle_d(
            STEP_2_67A,
            resolving_op="fs_scan:tasks/ mtime>started_at, untracked, keep-list-excluded",
            evidence={
                "method": "filesystem-mtime-scan",
                "started_at": started_at,
                "scratch_roots": ["tasks/"],
                "applied_exclusions": [
                    "git-tracked",
                    "keep-list:todo.md|plan.md|completion-log.md|*.plan.md",
                    "cross-repo/inbox:deferred-step-2.65-ordering",
                    (
                        "case-b:mitigated-by-untracked+mtime-filter;"
                        "full-sibling-scope-read-deferred"
                    ),
                ],
                "candidate_count": scratch_count,
                "scope_note": (
                    "deterministic subset of canonical enumeration "
                    "(canonical includes non-mechanizable 'EM working notes'); "
                    "STEP_2_67B/J does the full-judgment pass"
                ),
            },
        ))

    # -----------------------------------------------------------------------
    # Branch 12 — Completeness checklist present (D — chain-terminal only)
    # -----------------------------------------------------------------------
    completeness_present = False
    completeness_evidence: dict[str, Any] = {}
    if canonicalize(disposition) == PREDECESSOR_CONSUMED and consumed_handoff_fm:
        completeness_present = "completeness_checklist" in consumed_handoff_fm
        completeness_evidence = {
            "method": "handoff_frontmatter_read",
            "handoff_path": consumed_handoff_path,
            "completeness_checklist_present": completeness_present,
            "chain_terminal": True,
        }
    else:
        completeness_evidence = {
            "chain_terminal": canonicalize(disposition) == PREDECESSOR_CONSUMED,
            "consumed_handoff_path": consumed_handoff_path,
            "note": "single-session or no consumed handoff — step skipped",
        }

    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_COMPLETENESS_CHECKLIST,
        legible=True,
        node_type="D",
        signal_read=completeness_present,
        evidence=completeness_evidence,
    ))
    # (No separate step node for this — it gates Step 2.96)

    # -----------------------------------------------------------------------
    # Branch 13 — Checklist items verified (D)
    # -----------------------------------------------------------------------
    # Reads state/tasks/<sid>/completeness-checklist.yaml (C3).
    # completeness_present gate (Branch 12): when no checklist was ever created
    # (normal single-session case), the mirror is absent → D with zero/negative
    # signal is correct, not an error.
    # Review: code-reviewer — mirror is read regardless of completeness_present;
    # the mirror may exist even without the handoff frontmatter field (both arms
    # were identical — collapsed to single call).
    mirror_data: dict | None = _read_completeness_mirror(worktree_root, sid)

    if mirror_data is not None and "schema_mismatch" in mirror_data:
        checklist_signal: dict = {
            "schema_mismatch": mirror_data.get("schema_mismatch"),
            "open_count": None,
        }
        checklist_evidence: dict = {
            "method": f"read state/tasks/{sid}/completeness-checklist.yaml",
            "schema_mismatch": mirror_data.get("schema_mismatch"),
            "open_count": None,
            "total_count": None,
            "note": "schema mismatch — contract drift; inspect before acting",
        }
    elif mirror_data is not None:
        checklist_signal = {
            "open_count": mirror_data.get("open_count", 0),
            "total_count": mirror_data.get("total_count", 0),
        }
        checklist_evidence = {
            "method": f"read state/tasks/{sid}/completeness-checklist.yaml",
            "open_count": mirror_data.get("open_count", 0),
            "total_count": mirror_data.get("total_count", 0),
        }
    else:
        # Absent or no state: lines — zero/negative signal (normal case)
        checklist_signal = {"open_count": 0, "total_count": 0}
        checklist_evidence = {
            "method": f"read state/tasks/{sid}/completeness-checklist.yaml",
            "open_count": 0,
            "total_count": 0,
            "note": "mirror absent or no state: lines — zero/negative signal",
        }

    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_CHECKLIST_ITEMS,
        legible=True,
        node_type="D",
        signal_read=checklist_signal,
        evidence=checklist_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_2_96,
        resolving_op=f"read state/tasks/{sid}/completeness-checklist.yaml",
        evidence=checklist_evidence,
    ))

    # -----------------------------------------------------------------------
    # Branch 14 — Big-diff brightline (D) — already computed above for B1
    # -----------------------------------------------------------------------
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_BIG_DIFF_BRIGHTLINE,
        legible=True,
        node_type="D",
        signal_read=brightline_verdict,
        evidence={
            "method": "git_log_stat",
            "diff_loc": diff_loc,
            "commit_count": commit_count,
            "surface_count": surface_count,
            "verdict": brightline_verdict,
            "thresholds": b1_pre_resolved["thresholds"],
        },
    ))

    # -----------------------------------------------------------------------
    # Branch 15 — Plan-claim guard (D)
    # -----------------------------------------------------------------------
    # wsc_resolve reads claim state (evidence only); wsc_commit Step 2.4-a acquires.
    claim_verdict, claim_detail = _check_plan_claim(worktree_root, chain_slug)
    plan_claim_evidence = {
        "method": "filesystem:git-common-dir/coordinator-sessions/plan-claims",
        "governing_plan_slug": chain_slug,
        "verdict": claim_verdict,
        "detail": claim_detail,
    }
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_PLAN_CLAIM_GUARD,
        legible=True,
        node_type="D",
        signal_read=claim_verdict,
        evidence=plan_claim_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_2_4A,
        resolving_op="cs_claim_plan:read_state",
        evidence=plan_claim_evidence,
    ))
    ctx.add_node(handle_d(STEP_2_4B, resolving_op="disk-first:allowlist-edit",
                          # Review: code-reviewer — structured disk_first key lets a receipt
                          # auditor filter on structure rather than string-matching the note.
                          evidence={"note": "plan-reconciliation ALLOWLIST edit written in place at Step 2.4; op records provenance, not payload (Option B)",
                                    "disk_first": True}))

    # -----------------------------------------------------------------------
    # Branch 16 — LoE path (D) — directly from disposition
    # -----------------------------------------------------------------------
    loe_script = (
        "aggregate-chain-loe.sh"
        if canonicalize(disposition) == PREDECESSOR_CONSUMED
        else "coordinator-session-loe.sh"
    )
    loe_evidence = {
        "method": "disposition_direct",
        "disposition": disposition,
        "loe_script": loe_script,
        "consumed_handoff_path": consumed_handoff_path,
    }
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_LOE_PATH,
        legible=True,
        node_type="D",
        signal_read=loe_script,
        evidence=loe_evidence,
    ))
    ctx.add_node(handle_d(
        STEP_2_6_5A,
        resolving_op="disposition_direct:loe_fork",
        evidence=loe_evidence,
    ))

    # -----------------------------------------------------------------------
    # Remaining F-nodes (phase-1: empty slots for EM authorship)
    # -----------------------------------------------------------------------
    ctx.add_node(emit_f(STEP_2B, filled=""))
    ctx.add_node(emit_f(STEP_2_6_6C, filled=""))
    ctx.add_node(emit_f(STEP_4B, filled=""))

    # -----------------------------------------------------------------------
    # Remaining J-nodes (phase-1: empty answers for EM)
    # -----------------------------------------------------------------------
    ctx.add_node(emit_j(STEP_2_6_7, answer=""))
    ctx.add_node(emit_j(STEP_2_67B, answer=""))
    ctx.add_node(emit_j(STEP_2_8A, answer=""))
    ctx.add_node(emit_j(STEP_2_8C, answer=""))
    ctx.add_node(emit_j(STEP_2_95B, answer=""))

    # -----------------------------------------------------------------------
    # Remaining D-nodes (non-branch-discriminant steps)
    # -----------------------------------------------------------------------
    ctx.add_node(handle_d(STEP_1C, resolving_op="glob:state/lessons/*.yaml",
                          evidence={"note": "dedup pre-check: scan lesson titles"}))
    ctx.add_node(handle_d(STEP_1B, resolving_op="disk-first:coordinator-lesson-add",
                          # Review: code-reviewer — structured disk_first key lets a receipt
                          # auditor filter on structure rather than string-matching the note.
                          evidence={"note": "lesson authored disk-first at Step 1/1.2; op records provenance, not payload (Option B)",
                                    "disk_first": True}))
    ctx.add_node(handle_d(STEP_2_6_1, resolving_op="git_log:oneline+archive_scan",
                          evidence={"note": "scan session commits for substantive work"}))
    ctx.add_node(handle_d(STEP_2_6_2, resolving_op="git_mv:legacy_monolith",
                          evidence={"note": "auto-migrate legacy monolith (idempotent)"}))
    ctx.add_node(handle_d(STEP_2_6_5, resolving_op="string_op:sid6",
                          evidence={"note": "derive <sid6> from $WSC_SID (tail -c 7 | head -c 6)"}))
    ctx.add_node(handle_d(STEP_2_6_6A, resolving_op="coordinator-doc-new:scaffold",
                          evidence={"note": "scaffold completion entry skeleton"}))
    ctx.add_node(handle_d(STEP_2_6_6B, resolving_op="edit_fill:frontmatter_fields",
                          evidence={"note": "fill structured frontmatter from resolved steps"}))
    ctx.add_node(handle_d(STEP_2_6_8, resolving_op="reconcile-completion-commits.sh",
                          evidence={"note": "post-summary reconcile — fold new commits into entry"}))
    ctx.add_node(handle_d(
        STEP_2_65C,
        resolving_op="memo.transition:resolve+cs_sweep_actioned_memos+cs_sweep_terminal_plans",
        evidence={
            "note": (
                "flip named memos via memo.transition resolve (C2, per-memo disposition "
                "required, no hand frontmatter mutation); then sweep actioned memos + "
                "terminal plans"
            ),
        },
    ))
    ctx.add_node(handle_d(STEP_2_7, resolving_op="coordinator-handoff-archive.sh:stamp-only",
                          evidence={
                              "note": "stamp predecessor handoff shipped — chain-terminal only",
                              "chain_terminal": canonicalize(disposition) == PREDECESSOR_CONSUMED,
                              # C1: consumed_handoff_path threaded from Phase-1 resolution so
                              # wsc_commit can run the stamp without re-resolving the handoff.
                              # Empty string when non-chain-terminal (stamp op is skipped).
                              "consumed_handoff_path": (
                                  consumed_handoff_path if canonicalize(disposition) == PREDECESSOR_CONSUMED else ""
                              ),
                              # the Staff Engineer F2: _read_step_2_7_evidence reads THIS node (not
                              # STEP_0's), so the plural set must be threaded here too —
                              # populating only the STEP_0 evidence dict silently
                              # collapses every multi-handoff session back to the
                              # 1-handoff bug this plan fixes.
                              "consumed_handoffs_paths": (
                                  consumed_handoff_paths_all if canonicalize(disposition) == PREDECESSOR_CONSUMED else []
                              ),
                          }))
    ctx.add_node(handle_d(STEP_2_8B, resolving_op="regenerate-orientation-cache.sh",
                          evidence={"note": "append pinboard entry if J-step answered yes"}))
    ctx.add_node(handle_d(STEP_2_9C, resolving_op="review-coverage-gate.sh",
                          evidence={
                              "note": "coverage gate D-tail after B1 — chain-terminal only",
                              "chain_terminal": canonicalize(disposition) == PREDECESSOR_CONSUMED,
                          }))
    ctx.add_node(handle_d(STEP_2_9D, resolving_op="coordinator-write-review-trail.sh",
                          evidence={"note": "review trail write D-tail after B1"}))
    ctx.add_node(handle_d(STEP_2_9_OBS, resolving_op="classify-dispatch-shape.sh",
                          evidence={"note": "dispatch-shape observation (read-only, advisory)"}))
    ctx.add_node(handle_d(STEP_2_95A, resolving_op="check-machine-local-regeneratability.sh",
                          evidence={"note": "machine-local regeneratability sub-check"}))
    ctx.add_node(handle_d(STEP_3_0, resolving_op="dirty-tree-gate.sh",
                          evidence={"note": "pre-terminate dirty-tree gate"}))
    ctx.add_node(handle_d(STEP_3_1, resolving_op="git_add:explicit_paths",
                          evidence={"note": "stage session paths (explicit WSC_PATHS array)"}))
    ctx.add_node(handle_d(STEP_3_2, resolving_op="wsc-commit.sh",
                          evidence={"note": "commit via wsc-commit.sh + structural gate"}))
    ctx.add_node(handle_d(STEP_3_3, resolving_op="git_push:auto-push-hook",
                          evidence={"note": "verify remote synced + push"}))
    ctx.add_node(handle_d(STEP_3_5A, resolving_op="cs_archive",
                          evidence={"note": "archive session claim (idempotent)"},
                          tail_step=True))
    ctx.add_node(handle_d(STEP_3_5B, resolving_op="cs_release_artifact:plan",
                          evidence={"note": "release plan claim — governing-plan sessions only"},
                          tail_step=True))
    ctx.add_node(handle_d(STEP_4A, resolving_op="derived_from_prior_steps",
                          evidence={"note": "structured summary fields — all derived from prior outputs"}))

    # -----------------------------------------------------------------------
    # Declared membership signal (op-spec §3, Option B) — additive, NOT a
    # _resolve_branches conditional-emit refactor.  All 49 nodes above are
    # added unconditionally; membership here is the SAME predicate already
    # encoded per-node as evidence.chain_terminal, declared once as an
    # ordered top-level list rather than left as scavengeable evidence.
    # applicable_node_ids is a declared PipelineContext field (pipeline_context.py)
    # — serialized by to_dict()/reconstructed by from_dict(), so it survives the
    # phase-1 receipt round-trip into phase-2 (wsc_commit) intact.
    # -----------------------------------------------------------------------
    ctx.applicable_node_ids = [
        node["id"] for node in ctx.nodes
        if canonicalize(disposition) == PREDECESSOR_CONSUMED or node["id"] not in _CHAIN_TERMINAL_ONLY_STEPS
    ]

    return ctx


# ---------------------------------------------------------------------------
# Integration entrypoint (test-suite surface — NOT a registered op)
# ---------------------------------------------------------------------------


async def resolve_session_branches(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """Resolve a session's branch signals end-to-end and emit the evidence receipt.

    NOT a registered op.  This carried the retired
    ``@register_op("ceremony.wsc_resolve")`` registration until 2026-07-29; that op
    had no live caller and was removed, but the function survives as this module's
    single end-to-end entrypoint over the engine above — the seam the test suite
    drives, and the one place the read -> resolve -> emit sequence is expressed
    whole rather than as its three separate halves.

    Parameters (params dict):
        sid         (str, required) — the WSC session ID to resolve branches for.
        scope_mode  (str, optional) — override scope_mode; if absent, read from
                                      session-shape.json plan.scope_mode field.

    repo_root:
        Git common dir (from _OP_KEY_SCOPE = "common_dir").  Worktree root is
        derived via main_worktree_root(repo_root).

    Returns:
        {
          "exit_code":     0 | 1,
          "disposition":   "single-session" | "chain-terminal",
          "scope_mode":    str,
          "nature":        str,     # roadmap|bugfix|tech-debt|infra
          "resolved_state": dict,   # PipelineContext.to_dict()
          "applicable_node_ids": list[str],  # ordered step-IDs applicable to this
                                              # session's disposition (op-spec §3)
          "receipt_path":  str,     # repo-relative path to phase-1 receipt
          "j_questions":   list,    # [{id, question}] for EM presentation
          "f_slots":       list,    # [{id, slot}] for EM presentation
          "b_pre_resolved": dict,   # B1 pre_resolved_evidence blob
          "idempotency_guard_fired": bool,
          "open_memos_count": int,
        }

    On error (exit_code=1):
        {"exit_code": 1, "error": str}
    """
    # --- Parameter validation ---
    sid = params.get("sid", "")
    if not sid:
        return {"exit_code": 1, "error": "resolve_session_branches: 'sid' param is required"}

    if repo_root is None:
        return {
            "exit_code": 1,
            "error": (
                "resolve_session_branches: repo_root arg is None — "
                "common_dir not supplied by engine (check _OP_KEY_SCOPE = 'common_dir')"
            ),
        }

    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    # --- Read session-shape.json (Python-native; L1b fallback when absent) ---
    session_shape, shape_source = _read_session_shape(common_dir, sid)

    # Apply optional scope_mode override from params
    scope_override = str(params.get("scope_mode") or "")
    if scope_override and session_shape:
        # Inject param override into session_shape for downstream use
        session_shape.setdefault("plan", {})
        session_shape["plan"]["scope_mode"] = scope_override
    elif scope_override:
        session_shape = {"plan": {"scope_mode": scope_override}}
        shape_source = "param_override"

    # --- Resolve all branch signals ---
    ctx = _resolve_branches(
        worktree_root=worktree_root,
        common_dir=common_dir,
        sid=sid,
        session_shape=session_shape,
        shape_source=shape_source,
    )

    # --- Emit phase-1 receipt ---
    # Review: code-reviewer F7 — emit_receipt now returns (path, op_tail); op_tail unused here
    # C3 (2026-07-08-concurrency-safe-strangled-op-writes.md): sid threaded through so the
    # receipt lands on this session's shard (state/ceremony/wsc/<sid-short>-...json), not a
    # singleton two concurrent ceremonies would clobber.
    receipt_path, _ = emit_receipt(
        ctx,
        repo_root=worktree_root,
        sid=sid,
        tail_phase="archival",
        receipt_phase="phase-1",
    )

    # --- Derive summary fields for caller ---
    j_questions = [
        {"id": n["id"], "question": n.get("question", "")}
        for n in ctx.nodes
        if n.get("type") == "J"
    ]
    f_slots = [
        {"id": n["id"], "slot": n.get("slot", "")}
        for n in ctx.nodes
        if n.get("type") == "F"
    ]
    b_nodes = [n for n in ctx.nodes if n.get("type") == "B"]
    b_pre_resolved = b_nodes[0].get("pre_resolved_evidence", {}) if b_nodes else {}

    # Idempotency guard fired?
    idempotency_node = ctx.get_node(STEP_2_6_3A)
    idempotency_fired = bool(
        idempotency_node and idempotency_node.get("evidence", {}).get("fired")
    ) if idempotency_node else False

    # Open memos count
    open_memos_node = ctx.get_node(STEP_2_65A)
    open_memos_count = (
        open_memos_node.get("evidence", {}).get("open_count", 0)
        if open_memos_node else 0
    )

    # Nature
    nature_node = ctx.get_node(STEP_2_6_4)
    nature = nature_node.get("evidence", {}).get("nature", "") if nature_node else ""

    return {
        "exit_code": 0,
        "disposition": ctx.disposition,
        "scope_mode": ctx.scope_mode,
        "nature": nature,
        "resolved_state": ctx.to_dict(),
        "applicable_node_ids": ctx.applicable_node_ids,
        "receipt_path": rel_id(receipt_path, worktree_root),
        "j_questions": j_questions,
        "f_slots": f_slots,
        "b_pre_resolved": b_pre_resolved,
        "idempotency_guard_fired": idempotency_fired,
        "open_memos_count": open_memos_count,
    }
