"""
coordinator_core.ops.ceremony.tail_ops -- reused tail-op wiring + small cs_* native ports.

Purpose: in-process (no bash/node spawn) wiring of the already-Python ceremony tail ops --
``coverage.gate`` and ``review_trail.write`` -- plus native Python ports of the two remaining
``cs_*`` bash functions the OLD ``wsc_commit.py`` shelled out to via ``_run_cs_function``
(``bash -c "source coordinator-session.sh && <fn>"``): ``cs_archive`` and
``cs_release_artifact``. Also carries ``refresh_roadmap_callout`` -- the disposable STEP_2_75
sibling render (native ``refresh_roadmap_callout.main`` port), ported from the OLD
``wsc_commit.py``'s ``_tail_refresh_roadmap_callout`` and added here 2026-07-22 to close a C9
wiring gap. C9's orchestrator (``wsc_tail.py``) composes all of these helpers into the
single-pass pipeline; this module registers no top-level JSON-RPC op of its own.

The former sibling, ``render_handoff_tracker`` (in-process port of the retired
``render-handoff-tracker.js`` tracker render), was removed along with
``renderers.render_repo_section`` -- see ``docs/plans/
2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md`` § C2.

Fleet-op wiring follows the confirm-then-act contract (T1 preview ``dry_run:true`` -> T3 act
``dry_run:false``), resolved by public op-key string via ``coordinator_core.ipc.get_op_handler``
rather than importing each op module's private handler function -- a future op-module refactor
that drops the public registration surfaces cleanly as a ``None`` return, not an
``AttributeError`` at a private import site.

Archive-sweeps are DETACHED, not in-process (C2, 2026-07-23 plan
``docs/plans/2026-07-23-wsc-tail-slim-down.md``). ``fleet.archive_completed_plans``,
``fleet.archive_completed_handoffs``, and ``fleet.archive_actioned_memos`` used to be wired
here as blocking two-phase calls (``archive_completed_plans`` / ``archive_completed_handoffs``
/ ``sweep_actioned_memos``, now REMOVED -- 2769ms median of the ~3.3s ceremony, per the
plan's Baseline table); an execute-time occasion re-verification found the "these already run
elsewhere" duplication claim only partly true (no other occasion covers terminal plans or
actioned memos within a session, and a long session between SessionStarts would archive
nothing at all -- see plan § Execution Notes "Occasion map re-verified"). ``fire_archive_
sweeps_detached`` replaces the three blocking calls with four detached CLI fires
(``coordinator/bin/sweep-terminal-plans.py`` / ``sweep-shipped-handoffs.py`` /
``sweep-consumed-handoffs.py`` / ``sweep-actioned-memos.py``), preserving the whole budget
win (detached ⇒ ~0ms blocking) while keeping every archival class occasioned at every WSC
pass. Accepted latency, stated honestly: archival now completes shortly AFTER the ceremony
returns, not within it -- the detached child races the parent's own exit, not its commit.

Memo-sweep, when it runs, still goes through ``fleet.archive_actioned_memos`` -- verified
(plan § C6) as the semantic equivalent of the deleted ``cs_sweep_actioned_memos`` bash
function (archives ``status:actioned`` memos inbox->archive, flat, git-mv-stage-not-commit,
README skip, claim-safety live-holder skip, dest-collision skip). It is NOT the C8a
records-query helper -- the two are disjoint; C6 has no dependency on C8a/C8b/C8c. As of C2
it is invoked via the detached ``sweep-actioned-memos.py`` CLI, not an in-process call.

C5 (2026-07-23 wsc-tail-slim-down): ``refresh_roadmap_callout`` below was being dropped from
``wsc_tail.py``'s BLOCKING pre-commit tail and fired as a DETACHED CLI spawn instead, via
``fire_tracker_and_roadmap_detached`` (mirrors ``fire_archive_sweeps_detached``'s C2 shape
exactly). This function was never wired into ``wsc_tail.py`` (unreached dead code, historically
flagged as OPEN RESIDUE) and its tracker-CLI leg was removed 2026-08-14 (``docs/plans/
2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md`` § C2 residue sweep) --
it now fires the ``refresh-roadmap-callout.py`` CLI only. Its name is kept as-is; renaming it is
outside this residue-sweep's scope.

C6 (2026-07-23 wsc-tail-slim-down): ``run_coverage_gate`` below sheds coverage.gate's
BLOCKING-ness ONLY -- unlike C2/C5's archive-sweeps and tracker/roadmap renders, this is
NOT a ``detached_spawn.spawn_detached`` OS-process fire-and-forget. coverage.gate never
touches the tracked worktree or git index (it shells out to read-only ``git log`` and
persists its own artifact to the gitignored ``state/coverage/gate-result.json`` --
``coverage_gate.py``), so it carries none of the ``.git/index.lock`` / dirty-tree-gate
hazard C2 hit once (see that plan chunk's correction); it is safe to run concurrently with
the ceremony's own commit. The caller (``wsc_tail._run_precommit_tail``) fires this call via
``asyncio.create_task`` at the top of its own pre-commit tail and joins (awaits) it only at
the very end -- an ASYNCIO-level shed, not an OS-process one, chosen specifically because a
true detached child cannot hand its verdict back to THIS call's own receipt (see that
function's own docstring "coverage.gate concurrency" section for the full reasoning). The
verdict is therefore ALWAYS captured, in-process, in the same call that assembles the
receipt -- never dropped, never stale, unlike the archive-sweeps/tracker-roadmap sheds
(whose result contract is deliberately "spawn attempt only, never the eventual outcome").

Negative-spec:
    - Does NOT shell out to bash or ``coordinator-session.sh`` -- ``cs_archive`` /
      ``cs_release_artifact`` are native ``pathlib``/``shutil`` ports, not
      ``_run_cs_function`` subprocess bridges (AC1 / AC2).
    - Does NOT re-implement ``fleet.archive_actioned_memos``' terminality/skip logic here --
      any gap between it and the old ``cs_sweep_actioned_memos`` semantics is closed IN THAT
      OP, never duplicated in the tail (plan § C6).
    - ``cs_release_artifact`` does NOT key holder-identity on pid -- the bash ``pid``
      fallback branch is a permanent in-harness no-op (every hook Bash call gets a fresh
      ``$$``); this port keys exclusively on the claim dir's ``session_id`` file, never
      ``os.getpid()``. A claim dir with no ``session_id`` file (legacy pid-only claim) is
      therefore always treated as "not held by me" here -- self-heals to ``session_id`` on
      first takeover, same as the bash original.
    - ``cs_release_artifact`` does NOT decide the F1 exit-verdict ordering relative to the
      rest of the ceremony -- that sequencing question is C9's orchestrator design call (see
      ``wsc_tail.py``), not this module's.
    - Does NOT re-implement ``review_trail.write``'s session-id / workstream resolution here
      -- the op resolves those itself; this wrapper only supplies the caller-provided review
      metadata and gates on completeness.
    - ``write_review_trail`` is NOT shed by C6, and MUST NOT be shed as a follow-on "cleanup"
      pass without a fresh PM ruling. It has a live cross-repo contract (SKILL.md:1713,
      documenting the five ``--review-*`` fields' composition contract) and a SEQUENTIAL
      PREDECESSOR reader: DoE's chain-end ``review-coverage-gate.py`` (SKILL.md:28) requires
      the gate to emit ``VERDICT=COVERED`` before its own Step 3 may proceed. Shedding this
      write off the blocking path without first coordinating that change across the
      cross-repo boundary would silently race (or drop) the record that predecessor reads.
      DEFAULT stays SYNCHRONOUS: it is one small write, its cost is unmeasured until C1's
      real-shape baseline lands, and shedding it ahead of that measurement is speculative --
      see docs/plans/2026-07-23-wsc-tail-slim-down.md § C6 (the Director of Engineering, finding 9) for the full
      enumeration of readers this default protects.
    - ``fire_archive_sweeps_detached`` does NOT fire the composite ``coordinator/bin/
      sweep-boot.py`` -- that composite also runs the unintegrated-findings reap (a tracked
      ``git rm``), a destructive sweep out of scope for a call fired on every WSC pass
      (plan § C2). It fires the four per-class CLIs directly instead.
    - ``fire_archive_sweeps_detached`` does NOT stamp the shared ``archive_sweeps``
      housekeeping-liveness key -- a detached parent has no visibility into whether the
      spawned child actually succeeded, so each per-class CLI stamps its OWN success
      (mirrors ``sweep-boot.py``'s ``_stamp_archive_sweeps_liveness``; multiple producers
      stamping the same key is additive-safe by the key's own last-writer-wins contract,
      see ``housekeeping_liveness.stamp_liveness``).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C6
Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C2
Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C6
Behavior reference (read for behavior, not structure, per the plan's Anti-scope):
    Port of: coordinator-session.sh cs_archive, cs_release_artifact,
    _cs_claim_held_by_me (DoE e34f2484, 2026-07-22).
"""

from __future__ import annotations
import sys

# Generator-provenance declaration: this module wires/reuses other modules'
# writes (refresh_roadmap_callout, cs_archive/
# cs_release_artifact native ports, detached CLI fires) but performs no
# direct file write of its own to a tracked repo path -- every actual write
# site lives in the module it delegates to.
GENERATES = []

import asyncio
import contextlib
import io
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
from coordinator_core.ops.ceremony.housekeeping_liveness import (
    COVERAGE_GATE as _HL_COVERAGE_GATE,
    ROADMAP_CALLOUT as _HL_ROADMAP_CALLOUT,
    stamp_liveness,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id

# Import side-effect only: trigger each reused op module's @register_op(...) decorator so
# get_op_handler(...) below resolves via a direct registry hit rather than its lazy-import
# fallback (get_op_handler() self-resolves a MISS since 2026-07-25, so this pre-import is
# belt-and-braces, not strictly required for correctness) -- mirrors the
# "# noqa: F401 -- trigger registration" idiom used at every call site
# in the OLD wsc_commit.py). The three fleet archive ops (archive_plans/archive_handoffs/
# archive_actioned_memos) are NOT imported here for registration any more (C2) -- nothing in
# this module calls them in-process; the detached CLIs spawned by
# `fire_archive_sweeps_detached` register them in their OWN freshly-spawned process via
# `coordinator_core/ops/__init__.py`'s central registration list instead.
from coordinator_core.ops import coverage_gate as _coverage_gate_mod  # noqa: F401
from coordinator_core.ops import review_trail_write as _review_trail_write_mod  # noqa: F401
from coordinator_core.ops.review_trail_write import ForeignSessionRangeRefused

_LOG = logging.getLogger(__name__)

TailResult = Dict[str, Any]

# Public op keys this module wires in-process (AC11).
OP_COVERAGE_GATE = "coverage.gate"
OP_REVIEW_TRAIL = "review_trail.write"

# Results-dict label for the single detached-fire entry (C2) that replaced the three
# retired blocking OP_ARCHIVE_PLANS / OP_ARCHIVE_HANDOFFS / OP_ARCHIVE_MEMOS wrappers --
# see `fire_archive_sweeps_detached` and the module docstring's "Archive-sweeps are
# DETACHED" section.
OP_ARCHIVE_SWEEPS_DETACHED = "archive_sweeps:detached_fire"

# Native-port op labels (not JSON-RPC op keys -- these never go through get_op_handler).
OP_CS_ARCHIVE = "cs_archive"
OP_CS_RELEASE_ARTIFACT = "cs_release_artifact"

# review_trail.write's required params -- a caller-contract completeness gate before the
# op is ever invoked (mirrors the OLD wsc_commit.py:_tail_review_trail gate, ported in-process).
_REVIEW_TRAIL_REQUIRED_FIELDS = ("sha_range", "reviewer", "scope", "verdict", "diff_loc")

#: Public alias of the tuple above, for the cross-module callers that need to
#: NAME the required fields in their own diagnostic (`ceremony.wsc_tail`'s
#: pre-commit supply gate). Same object, never a copy -- widening one widens
#: both by construction.
REVIEW_TRAIL_REQUIRED_FIELDS = _REVIEW_TRAIL_REQUIRED_FIELDS


def _empty_result() -> TailResult:
    return {"acted": [], "skipped": [], "failed": []}


def _fail(op_label: str, reason: str) -> TailResult:
    return {"acted": [], "skipped": [], "failed": [f"{op_label}: {reason}"]}


def _ids(items: List[Any]) -> List[str]:
    """Normalize a fleet-op result list (dicts with 'id', or bare strings) to str ids.

    Review nit fix: a dict lacking BOTH 'id' and 'path' falls back to
    `str(item)` (a Python-repr of the whole dict) -- widened from a bare
    `.get("id", str(item))` so a `path`-only fleet-op item (no 'id' key)
    still yields a sensible identifier instead of degrading straight to a
    repr. All currently-reused fleet ops emit 'id'-bearing dicts, so this is
    defensive against future fleet-op shape drift, not a live bug today.
    """
    return [
        (item.get("id") or item.get("path") or str(item)) if isinstance(item, dict) else str(item)
        for item in items
    ]


def fleet_result_to_tail(result: dict, op_label: str) -> TailResult:
    """Extract acted/skipped/failed string lists from a fleet-op result envelope.

    Treats ``exit_code`` not in ``{0, 2}`` as an op-level failure when ``failed`` is
    otherwise empty (exit_code 2 is the fleet-op partial-failure code -- individual
    per-candidate failures are already reflected in the ``failed`` list itself).
    """
    acted = _ids(result.get("acted", []))
    skipped = _ids(result.get("skipped", []))
    failed = _ids(result.get("failed", []))

    ec = result.get("exit_code", 0)
    if ec not in (0, 2) and not failed:
        failed.append(f"{op_label}: exit_code={ec}")

    return {"acted": acted, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Fleet ops -- confirm-then-act (T1 preview -> T3 act) two-phase wiring
# ---------------------------------------------------------------------------


async def run_fleet_op_two_phase(
    handler_fn: Callable[..., Awaitable[dict]],
    op_label: str,
    common_dir: Path,
) -> TailResult:
    """Run a ``fleet.*`` op (``dry_run:true`` preview -> ``dry_run:false`` act) best-effort.

    Follows the confirmed-then-act fleet-op contract (contract §2.2):
        T1 preview (``dry_run:true``)  -> discover candidates
        T3 act     (``dry_run:false``) -> act on the discovered ``candidate_ids``

    When T1 returns no candidates, returns an all-empty result -- no T3 call is made.

    Never raises -- op-level exceptions and non-{0,2} exit codes are captured into
    ``failed[]`` so a single mis-wired reused op cannot abort the rest of the tail.
    """
    try:
        preview = await handler_fn(
            {"mode": "already-terminal", "dry_run": True},
            repo_root=common_dir,
        )
        if preview.get("exit_code", 0) != 0:
            return _fail(op_label, f"preview exit_code={preview.get('exit_code')}")

        candidates = _ids(preview.get("candidates", []))
        if not candidates:
            return _empty_result()

        act = await handler_fn(
            {
                "mode": "already-terminal",
                "dry_run": False,
                "candidate_ids": candidates,
            },
            repo_root=common_dir,
        )
        return fleet_result_to_tail(act, op_label)

    except Exception as exc:  # noqa: BLE001 -- best-effort tail op, never raises
        _LOG.warning("tail_ops: %s raised %s: %s", op_label, type(exc).__name__, exc)
        return _fail(op_label, f"{type(exc).__name__} -- {str(exc)[:160]}")


async def _run_fleet_op_by_key(op_key: str, op_label: str, common_dir: Path) -> TailResult:
    handler = get_op_handler(op_key)
    if handler is None:
        return _fail(op_label, f"{op_key} not registered")
    return await run_fleet_op_two_phase(handler, op_label, common_dir)


# The per-class on-demand CLIs `fire_archive_sweeps_detached` fires, relative to
# ``<worktree_root>/coordinator/bin/``. Deliberately mirrors
# `housekeeping_liveness.REMEDY_COMMANDS[ARCHIVE_SWEEPS]` (same four scripts) -- NOT the
# composite `sweep-boot.py` (module docstring negative-spec; plan § C2 anti-scope).
_ARCHIVE_SWEEP_SCRIPTS: tuple = (
    "sweep-terminal-plans.py",     # fleet.archive_completed_plans -- retired blocking OP_ARCHIVE_PLANS
    "sweep-shipped-handoffs.py",   # fleet.archive_completed_handoffs, deployment_state branch (shipped/abandoned/superseded)
    "sweep-consumed-handoffs.py",  # fleet.archive_completed_handoffs, claimed/consumed branch -- stronger DR-084 path (C21)
    "sweep-actioned-memos.py",     # fleet.archive_actioned_memos -- retired blocking OP_ARCHIVE_MEMOS
)


def fire_archive_sweeps_detached(worktree_root: Path) -> TailResult:
    """Fire the four per-class archive-sweep CLIs detached (C2) -- the replacement for the
    three retired blocking calls (``archive_completed_plans`` / ``archive_completed_handoffs``
    / ``sweep_actioned_memos``, all removed).

    Each CLI is spawned via `detached_spawn.spawn_detached` (fire-and-forget, effectively
    0ms blocking) with ``str(worktree_root)`` as its positional ``repo_root`` arg -- the
    same shape a human invokes it with directly (``python3 coordinator/bin/sweep-X.py``).
    The spawned child resolves its own params, runs its own two-phase fleet-op dance,
    self-commits its own archival mutation, and stamps the shared ``archive_sweeps``
    housekeeping-liveness key on its OWN success path -- this function does NOT stamp
    liveness itself (see module docstring negative-spec: a detached parent has no
    visibility into whether the spawned child actually succeeded).

    Returns a `TailResult` recording only the SPAWN attempt outcome (whether `Popen`
    itself was invoked without raising), NOT the eventual archival outcome -- that is
    invisible to this (synchronous, non-blocking) caller by construction. A spawn
    failure is recorded both here (``failed``) and in the shared housekeeping-failures
    log by `spawn_detached` itself (the parent-side "could not even start you" record).
    """
    result: TailResult = _empty_result()
    bin_dir = Path(worktree_root, "coordinator", "bin")
    repo_root_str = str(worktree_root)
    for script_name in _ARCHIVE_SWEEP_SCRIPTS:
        script_path = str(bin_dir / script_name)
        if spawn_detached(repo_root_str, script_path, [repo_root_str]):
            result["acted"].append(f"detached_fire:{script_name}")
        else:
            result["failed"].append(f"detached_fire:{script_name}: spawn_detached returned False")
    return result


# ---------------------------------------------------------------------------
# refresh-roadmap-callout -- disposable sibling render
# (STEP_2_75, C9 wiring-gap fix, 2026-07-22 -- see wsc_tail.py module docstring)
# ---------------------------------------------------------------------------

#: Native-port op label (not a JSON-RPC op key -- never goes through get_op_handler),
#: mirroring the OLD wsc_commit.py's ``_OP_ROADMAP_CALLOUT``.
OP_ROADMAP_CALLOUT = "node:refresh-roadmap-callout.sh"

# roadmap_id is attacker-influenceable frontmatter on a shared work/* branch and is
# interpolated into a subprocess arg -- mirrors the DoE pickup skill's allowlist guard.
# Imported (not re-compiled) from renderers.py, which owns the single canonical
# copy -- see that module's C8c negative-spec for why this must never be a second
# compiled definition.
from coordinator_core.ops.ceremony.renderers import _ROADMAP_ID_ALLOWLIST_RE  # noqa: E402


def refresh_roadmap_callout(worktree_root: Path, consumed_handoff_paths: List[str]) -> TailResult:
    """Refresh each consumed handoff's roadmap STUB-INDEX query-callout (STEP_2_75, op 4b).

    Formerly merged into the same STEP_2_75 D-node as the retired ``render_handoff_tracker``
    sibling (mirrors the OLD ``wsc_commit.py``'s Op 4 merge); that sibling was retired
    2026-08-14, see docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-
    renders.md § C2. Disposable render nicety -- never hard-fails the tail; every failure
    mode here degrades to a clean skip/soft-fail.

    Gate: only runs (per handoff) when the consumed handoff carries a non-empty ``roadmap_id``
    in its frontmatter -- one ``refresh-roadmap-callout`` call per handoff that has one
    (per-handoff render).

    Security: ``roadmap_id`` is attacker-influenceable frontmatter on a shared ``work/*``
    branch and is interpolated into a subprocess arg -- gated through
    ``_ROADMAP_ID_ALLOWLIST_RE`` (bare identifier, no "..", no path separators) before use,
    mirroring the DoE pickup skill's allowlist guard.

    Ported from the OLD ``wsc_commit.py``'s ``_tail_refresh_roadmap_callout`` -- widened here
    to loop over the caller-supplied ``consumed_handoff_paths`` list directly (C9's
    orchestrator resolves the consumed set once, up front, via ``find_all_consumed_handoffs``,
    rather than threading it through a per-node ``PipelineContext`` read).

    Returns {acted, skipped, failed} plus ``roadmap_stub_index_paths`` -- the repo-relative
    ``state/roadmap/<roadmap_id>/STUB-INDEX.md`` path for every roadmap successfully refreshed
    (whether an actual rewrite or the module's own idempotent no-op). The caller folds these
    into ``extra_stage_paths`` -- ``refresh_roadmap_callout.main`` rewrites a TRACKED file
    in-place, so an actual rewrite is a dirty-tree-gate "unattributable" hit unless explicitly
    staged (the retired ``render_handoff_tracker`` carried the same staging note before it
    was removed); staging an unmodified file is a harmless idempotent ``git add`` no-op.

    No ``consumed_handoff_paths`` at all (no chain continuation this pass) is a clean skip,
    not a failure.
    """
    if not consumed_handoff_paths:
        return {"acted": [], "skipped": [f"{OP_ROADMAP_CALLOUT}:no-consumed-handoff"], "failed": []}

    from coordinator_core.ops.refresh_roadmap_callout import main as _refresh_roadmap_callout_main

    acted: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    stub_index_paths: List[str] = []

    for consumed_handoff_path in consumed_handoff_paths:
        handoff_abs = worktree_root / consumed_handoff_path
        try:
            content = handoff_abs.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skipped.append(
                f"{OP_ROADMAP_CALLOUT}:handoff-not-readable:{consumed_handoff_path}:{exc}"
            )
            continue

        roadmap_id = ""
        for line in content.splitlines():
            if line.startswith("roadmap_id:"):
                raw = line[len("roadmap_id:"):].strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("\"", "'"):
                    raw = raw[1:-1]
                roadmap_id = raw
                break

        if not roadmap_id or ".." in roadmap_id or not _ROADMAP_ID_ALLOWLIST_RE.match(roadmap_id):
            skipped.append(f"{OP_ROADMAP_CALLOUT}:no-roadmap-id:{consumed_handoff_path}")
            continue

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
                rc = _refresh_roadmap_callout_main([roadmap_id, "--root", str(worktree_root)])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001 -- best-effort tail op, never raises
            failed.append(f"{OP_ROADMAP_CALLOUT}: {type(exc).__name__} -- {str(exc)[:160]}")
            continue

        if rc == 0:
            acted.append(f"{OP_ROADMAP_CALLOUT}:{roadmap_id}")
            stub_index_rel = f"state/roadmap/{roadmap_id}/STUB-INDEX.md"
            if (worktree_root / stub_index_rel).is_file():
                stub_index_paths.append(stub_index_rel)
        else:
            reason = (stderr_buf.getvalue().strip() or stdout_buf.getvalue().strip() or f"exit_code={rc}")[:200]
            failed.append(f"{OP_ROADMAP_CALLOUT}: {reason}")

    # Success-path-only liveness stamp (ROADMAP_CALLOUT): only when at least one
    # consumed handoff's roadmap callout was actually refreshed this pass --
    # an all-skipped loop (no roadmap_id anywhere, or every callout already
    # up-to-date) or an all-failed loop must NOT read as "the class ran".
    if acted:
        stamp_liveness(str(worktree_root), _HL_ROADMAP_CALLOUT)

    return {
        "acted": acted, "skipped": skipped, "failed": failed,
        "roadmap_stub_index_paths": stub_index_paths,
    }


# ---------------------------------------------------------------------------
# fire_tracker_and_roadmap_detached -- C5 (2026-07-23 wsc-tail-slim-down):
# render_handoff_tracker + refresh_roadmap_callout were dropped from the BLOCKING
# wsc_tail.py pre-commit path and fired as DETACHED CLI spawns instead.
# render_handoff_tracker itself was later retired outright 2026-08-14 (see
# docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md
# § C2); refresh_roadmap_callout above remains the live call target of the CURRENT
# wsc_tail.py pre-commit tail until that module's own C5 edit repoints its STEP_2_75
# call site onto this detached-fire function (see this module's own docstring "C5"
# section, and the executor report that landed this function, for the precise
# before/after wsc_tail.py needs).
# ---------------------------------------------------------------------------

#: bin/ CLI name (relative to ``<worktree_root>/coordinator/bin/``) this function
#: spawns detached -- the SAME occasion CLI `/handoff` SKILL.md and workday-start
#: already invoke standalone (module docstring "C5"), reused here rather than a
#: second, WSC-only spawn mechanism.
_ROADMAP_CALLOUT_CLI_SCRIPT = "refresh-roadmap-callout.py"


def _consumed_handoff_roadmap_ids(worktree_root: Path, consumed_handoff_paths: List[str]) -> List[str]:
    """Extract distinct, allowlist-valid ``roadmap_id``s from consumed handoffs' frontmatter.

    Read-only extraction, no render -- the render itself is now the detached
    ``refresh-roadmap-callout.py`` CLI's job (see `fire_tracker_and_roadmap_detached`),
    not this function's. Mirrors `refresh_roadmap_callout`'s own per-handoff
    frontmatter scan and `_ROADMAP_ID_ALLOWLIST_RE` gate above, minus the actual
    render call. Order-preserving de-dup (first occurrence wins) -- firing the same
    roadmap_id's callout refresh twice in one pass would be a harmless idempotent
    no-op but a wasted extra detached cold-start (see this function's caller
    docstring for the "detaching doesn't remove the cost" honesty note).
    """
    seen: List[str] = []
    for consumed_handoff_path in consumed_handoff_paths:
        handoff_abs = worktree_root / consumed_handoff_path
        try:
            content = handoff_abs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        roadmap_id = ""
        for line in content.splitlines():
            if line.startswith("roadmap_id:"):
                raw = line[len("roadmap_id:"):].strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("\"", "'"):
                    raw = raw[1:-1]
                roadmap_id = raw
                break

        if not roadmap_id or ".." in roadmap_id or not _ROADMAP_ID_ALLOWLIST_RE.match(roadmap_id):
            continue
        if roadmap_id not in seen:
            seen.append(roadmap_id)
    return seen


def fire_tracker_and_roadmap_detached(
    worktree_root: Path, consumed_handoff_paths: List[str]
) -> TailResult:
    """Fire the per-roadmap callout refresh DETACHED (C5) -- the intended replacement
    for the retired BLOCKING in-process `refresh_roadmap_callout` call above (kept for
    the CURRENT wsc_tail.py call site until its own C5 edit repoints STEP_2_75 onto
    this function -- see module docstring). Mirrors `fire_archive_sweeps_detached`'s
    shape exactly (C2 precedent) -- same `spawn_detached` seam, same "record the SPAWN
    attempt only" result contract, no second spawn mechanism invented.

    Its former handoff-tracker render leg was removed 2026-08-14 along with the
    renderer it fired (`docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-
    tracker-renders.md` § C2). Fires `refresh-roadmap-callout.py` once per distinct
    allowlist-valid `roadmap_id` found in `consumed_handoff_paths`' frontmatter
    (`_consumed_handoff_roadmap_ids`) -- a clean `skipped[]` entry, not a failure, when
    no consumed handoff carries a roadmap_id.

    ASYNC-IS-NOT-A-SUBSTITUTE-FOR-CHEAP (the Staff Engineer finding 19, module docstring honesty
    note): detaching moves the cost of this render -- a Python cold start plus the
    render's own filesystem walk -- OFF this function's own (previously blocking)
    turn, but does NOT remove that cost from existing anywhere; the render now runs
    CONCURRENTLY with whatever the caller does next (the ceremony's own post-commit
    steps, or a fresh session's next command), racing it rather than stalling it. That
    is the accepted, explicitly-stated tradeoff this chunk trades for (the render's
    own walk is small and bounded, unlike C18's).

    ARTIFACT-DISPOSITION RESIDUE (C5, NOT closed by this function alone -- see the
    executor report that landed this): `refresh-roadmap-callout.py` does not self-commit
    its written/rewritten output today -- it is a pure render/rewrite CLI with no git
    operations of its own. The C5 plan text requires the detached invocation to
    "commit its own output with an explicit pathspec, in a small follow-up commit" for
    its in-place STUB-INDEX rewrite (always a TRACKED file). Adding that self-commit
    step requires editing the `coordinator/bin/` CLI trampoline (or its underlying
    `coordinator_core.ops.refresh_roadmap_callout` module) -- a file outside this
    dispatch's declared `tail_ops.py`-only surface. This function fires the refresh
    detached exactly as specified; the commit-own-output half of the disposition is
    NOT yet implemented anywhere and remains open until that follow-up edit lands (see
    negative-spec below).

    Returns a `TailResult` recording only the SPAWN attempt outcome, never the
    eventual render/rewrite/commit outcome -- identical contract to
    `fire_archive_sweeps_detached` (see that function's own docstring).

    Negative-spec:
        - Does NOT commit the CLI's output -- see "ARTIFACT-DISPOSITION RESIDUE"
          above. A caller relying on this function alone to close the dirty-tree-gate
          hazard the C2-era `extra_stage_paths` coupling used to close is NOT yet
          fully served; that gap must close before the pre-commit
          `refresh_roadmap_callout` call site is dropped.
        - Does NOT decide WHEN (relative to the ceremony's own commit) this function
          is called -- that is the caller's (`wsc_tail.py`'s) sequencing call. It MUST
          be called strictly AFTER the ceremony's own commit has landed, mirroring
          `fire_archive_sweeps_detached`'s own post-commit call site (gated on
          `committed_sha is not None`) -- calling it pre-commit reopens the exact
          `.git/index.lock` contention / mid-write dirty-file hazard C2 already hit
          once (module docstring "C2 correction").
        - Does NOT invent a second spawn mechanism, failures-log format, or liveness
          store -- reuses `detached_spawn.spawn_detached` verbatim (C5 remit).
    """
    result: TailResult = _empty_result()
    bin_dir = Path(worktree_root, "coordinator", "bin")
    repo_root_str = str(worktree_root)

    roadmap_ids = _consumed_handoff_roadmap_ids(worktree_root, consumed_handoff_paths)
    if not roadmap_ids:
        result["skipped"].append(f"detached_fire:{_ROADMAP_CALLOUT_CLI_SCRIPT}:no-roadmap-id")
    for roadmap_id in roadmap_ids:
        callout_script = str(bin_dir / _ROADMAP_CALLOUT_CLI_SCRIPT)
        if spawn_detached(repo_root_str, callout_script, ["--root", repo_root_str, roadmap_id]):
            result["acted"].append(f"detached_fire:{_ROADMAP_CALLOUT_CLI_SCRIPT}:{roadmap_id}")
        else:
            result["failed"].append(
                f"detached_fire:{_ROADMAP_CALLOUT_CLI_SCRIPT}:{roadmap_id}: spawn_detached returned False"
            )
    return result


# ---------------------------------------------------------------------------
# coverage.gate -- single-call, in-process, best-effort
# ---------------------------------------------------------------------------


async def run_coverage_gate(
    common_dir: Path,
    *,
    closing_session_id: str = "",
    range_arg: str = "",
    from_handoff: str = "",
    scope_paths: Optional[List[str]] = None,
) -> TailResult:
    """Wire ``coverage.gate`` in-process, best-effort.

    ``COVERED``/``UNCOVERED`` verdicts land in ``acted[]`` (tagged with the verdict
    token); ``INDETERMINATE`` (``exit_code == 2``) lands in ``failed[]`` -- the ceremony
    continues either way, this is D-node evidence only, never a hard gate.
    """
    handler = get_op_handler(OP_COVERAGE_GATE)
    if handler is None:
        return _fail(OP_COVERAGE_GATE, f"{OP_COVERAGE_GATE} not registered")

    params: Dict[str, Any] = {"closing_session_id": closing_session_id}
    if range_arg:
        params["range"] = range_arg
    if from_handoff:
        params["from_handoff"] = from_handoff
    if scope_paths:
        params["scope_paths"] = scope_paths

    try:
        result = await handler(params, repo_root=common_dir)
        ec = result.get("exit_code", 1)
        verdict_line = result.get("verdict_line", "")
        if ec == 0:
            token = (
                verdict_line.split("VERDICT=")[-1].split()[0]
                if "VERDICT=" in verdict_line
                else verdict_line
            )
            # Success-path-only liveness stamp (COVERAGE_GATE): exit_code == 0
            # means the gate produced a real COVERED/UNCOVERED verdict. Stamped
            # against `main_worktree_root(common_dir)`, NOT `common_dir` itself --
            # `common_dir` is `.git`-internal (the coverage.gate handler above is
            # called with `repo_root=common_dir` for ITS own purposes, but the
            # liveness stamp must land at the worktree root, which is the only
            # path `housekeeping_liveness.liveness_path`'s predicate accepts and
            # the only path `orientation/regenerate_cache.py`'s reader ever reads).
            # INDETERMINATE (ec == 2) and any other error fall through to the
            # _fail(...) branches below and do NOT stamp.
            stamp_liveness(str(main_worktree_root(common_dir)), _HL_COVERAGE_GATE)
            return {"acted": [f"{OP_COVERAGE_GATE}:{token}"], "skipped": [], "failed": []}
        if ec == 2:
            notes = "; ".join(str(n) for n in result.get("notes", []))
            return _fail(OP_COVERAGE_GATE, f"INDETERMINATE -- {notes[:200]}")
        return _fail(OP_COVERAGE_GATE, f"error exit_code={ec}")
    except Exception as exc:  # noqa: BLE001 -- best-effort tail op, never raises
        _LOG.warning("tail_ops: coverage.gate raised %s: %s", type(exc).__name__, exc)
        return _fail(OP_COVERAGE_GATE, f"{type(exc).__name__} -- {str(exc)[:160]}")


# ---------------------------------------------------------------------------
# review_trail.write -- single-call, in-process, best-effort, metadata-gated
# ---------------------------------------------------------------------------


def review_trail_metadata_complete(review_trail: "Optional[Union[dict, list]]") -> bool:
    """Whether ``review_trail`` carries at least one writable record, for BOTH
    accepted shapes -- a single dict with all of ``_REVIEW_TRAIL_REQUIRED_
    FIELDS`` present and non-empty, or a list with >=1 such entry.

    Extracted so a caller can decide the ``b_adjudication_present`` breach
    BEFORE mutating anything (``ceremony.wsc_tail``'s pre-commit refusal),
    using the exact predicate ``write_review_trail`` / ``write_review_trail_
    many`` apply to the same input one step later. Pure: reads only the value
    handed to it, no disk, no git.

    Negative-spec: this is a SUPPLY check, never a write-outcome check. A
    complete payload whose write is then refused (foreign-session range) or
    fails still returns ``True`` here -- those surface through the write
    functions' own ``failed``/``failed_critical`` entries, which no pre-flight
    can anticipate.
    """
    if isinstance(review_trail, list):
        return any(
            isinstance(entry, dict)
            and all(entry.get(k) not in (None, "") for k in _REVIEW_TRAIL_REQUIRED_FIELDS)
            for entry in review_trail
        )
    if not isinstance(review_trail, dict):
        return False
    return all(review_trail.get(k) not in (None, "") for k in _REVIEW_TRAIL_REQUIRED_FIELDS)


async def write_review_trail(
    common_dir: Path,
    review_trail: Optional[dict] = None,
    *,
    b_adjudication_present: bool = False,
    partition_mandatory: bool = False,
    _batch_context: Optional[dict] = None,
) -> TailResult:
    """Wire ``review_trail.write`` in-process, metadata-gated, best-effort.

    The op REQUIRES ``sha_range``/``reviewer``/``scope``/``verdict``/``diff_loc``. When the
    caller supplies complete metadata the call is forwarded; when metadata is absent or
    incomplete the call is SKIPPED cleanly (never an incomplete-params invocation that
    fails on missing required fields). ``scope_kind``/``workstream`` are optional passthrough.

    ``b_adjudication_present`` gate (ported from the OLD wsc_commit.py ``_tail_review_trail``
    F6 fix): when a reviewed workstream-close passed adjudication but ``review_trail`` is
    absent or incomplete, that is a caller-contract breach -- a reviewed close-out MUST also
    supply complete review-trail metadata, or the trail is silently lost. This surfaces as a
    ``failed_critical[]`` entry (distinct from ``failed[]``) so C9's exit-code predicate can
    treat it as CRITICAL rather than a clean best-effort skip. When
    ``b_adjudication_present=False`` and ``partition_mandatory=False``, incomplete metadata
    is the ordinary "no review this session" skip.

    ``partition_mandatory`` (D, cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-trail-
    skips-silently.md): sibling gate to ``b_adjudication_present`` above, same
    ``failed_critical`` outcome on incomplete metadata -- a resolved ``decide_review_scale``
    row 4/6 (mandatory partitioned review) is as strong a statement that a review was owed
    as an adjudication assertion is. The two gates are evaluated with OR, not independently
    reported: either one alone is sufficient to make incomplete metadata critical, and the
    caller (``ceremony.wsc_tail``) is expected to pass at most a coherent combination of the
    two, not to rely on this function to reconcile a contradiction between them.

    A caller-supplied ``sha_range`` that trips the write-side foreign-session
    scope guard (``review_trail_write._guard_foreign_session_range``) is
    ANOTHER ``failed_critical[]`` case, distinct from the adjudication gate
    above: ``review_trail_write.ForeignSessionRangeRefused`` is caught before
    the blanket ``except Exception`` below and returned untruncated, so it is
    both distinguishable from an ordinary write error and never clipped to
    160 chars (docs/plans/2026-07-27-review-trail-scope-guard.md § AC9). Every
    other exception from the handler keeps the existing truncated
    ``failed[]`` treatment unchanged. This function's "never raises,
    best-effort" contract is otherwise unchanged.

    Every returned dict also carries ``metadata_supplied`` (bool) -- whether
    the CALLER'S input was complete, independent of whether the write itself
    then succeeded, was refused, or raised. Review: staff-eng 2026-08-14 --
    before this field existed, ``ceremony.wsc_tail``'s receipt derived
    "was review metadata supplied" from the ABSENCE of the
    ``no-review-metadata`` skip entry, which this function's own
    ``b_adjudication_present``-incomplete branch never emits (it returns
    ``failed_critical`` instead, per F6, deliberately not a skip -- see
    ``test_review_trail_b_adjudication_incomplete_is_critical``). That made
    the receipt read ``review_metadata_supplied=True`` on the exact input
    (incomplete metadata) it exists to flag as ``False``. ``metadata_supplied``
    is the direct, unambiguous fact instead of an absence-of-skip inference.
    """
    review_trail = review_trail or {}
    complete = all(
        review_trail.get(field) not in (None, "") for field in _REVIEW_TRAIL_REQUIRED_FIELDS
    )

    if not complete:
        if b_adjudication_present or partition_mandatory:
            fields_str = ", ".join(_REVIEW_TRAIL_REQUIRED_FIELDS)
            reason = (
                "b_adjudication present" if b_adjudication_present else "partition_mandatory resolved"
            )
            msg = (
                f"{OP_REVIEW_TRAIL}: {reason} but review_trail missing "
                f"required fields: {fields_str}"
            )
            return {"acted": [], "skipped": [], "failed": [], "failed_critical": [msg], "metadata_supplied": False}
        return {
            "acted": [], "skipped": [f"{OP_REVIEW_TRAIL}:no-review-metadata"], "failed": [],
            "metadata_supplied": False,
        }

    handler = get_op_handler(OP_REVIEW_TRAIL)
    if handler is None:
        result = _fail(OP_REVIEW_TRAIL, f"{OP_REVIEW_TRAIL} not registered")
        result["metadata_supplied"] = True
        return result

    params: Dict[str, Any] = {
        "sha_range": review_trail["sha_range"],
        "reviewer": review_trail["reviewer"],
        "scope": review_trail["scope"],
        "verdict": review_trail["verdict"],
        "diff_loc": review_trail["diff_loc"],
    }
    if review_trail.get("scope_kind"):
        params["scope_kind"] = review_trail["scope_kind"]
    if review_trail.get("workstream"):
        params["workstream"] = review_trail["workstream"]
    # Optional passthrough, same shape as scope_kind/workstream above: the path
    # set the review actually covered. Forwarded only when the caller supplies
    # it — the writer omits the key entirely on non-diff scope_kinds.
    # Spec: docs/plans/2026-07-27-review-trail-scope-guard.md § C9.
    if review_trail.get("reviewed_paths"):
        params["reviewed_paths"] = review_trail["reviewed_paths"]
    # Evidence correlating `reviewer` with an artifact showing a review
    # occurred -- enforced op-side (review_trail_write._verify_reviewer_evidence);
    # this tail-op only forwards it verbatim.
    # Spec: state/bug-backlog/2026-08-10-coordinator-write-review-trail-accepts-a-295d3cd80d13.yaml
    if review_trail.get("reviewer_evidence"):
        params["reviewer_evidence"] = review_trail["reviewer_evidence"]
    # C1 (docs/plans/2026-08-15-the-review-trail-write-stops-paying-n-wa.md):
    # internal-only in-process passthrough of a `write_review_trail_many`-
    # built batch attribution context (`review_trail_write.build_batch_
    # attribution_context`) -- never a real JSON-RPC param, see
    # `_review_trail_write_handler`'s own comment at its forwarding site.
    # `None` (the single-call `write_review_trail` default) is a no-op --
    # every consumer already falls back to its original per-call behavior.
    if _batch_context:
        params["_batch_context"] = _batch_context

    try:
        result = await handler(params, repo_root=common_dir)
        out_path = result.get("out_path", "")
        return {
            "acted": [f"{OP_REVIEW_TRAIL}:{out_path}"], "skipped": [], "failed": [],
            "metadata_supplied": True,
        }
    except ForeignSessionRangeRefused as exc:
        # AC9: a caller-supplied sha_range that trips the write-side
        # foreign-session scope guard MUST surface as a distinguishable,
        # UNTRUNCATED failed_critical[] entry -- not a truncated failed[]
        # message indistinguishable from an ordinary write error. Caught
        # before the blanket except Exception below on purpose.
        _LOG.warning("tail_ops: review_trail.write refused foreign-session range: %s", exc)
        return {
            "acted": [], "skipped": [], "failed": [], "failed_critical": [f"{OP_REVIEW_TRAIL}: {exc}"],
            "metadata_supplied": True,
        }
    except Exception as exc:  # noqa: BLE001 -- best-effort tail op, never raises
        _LOG.warning("tail_ops: review_trail.write raised %s: %s", type(exc).__name__, exc)
        result = _fail(OP_REVIEW_TRAIL, f"{type(exc).__name__} -- {str(exc)[:160]}")
        result["metadata_supplied"] = True
        return result


async def write_review_trail_many(
    common_dir: Path,
    review_trail_list: Optional[list],
    *,
    b_adjudication_present: bool = False,
    partition_mandatory: bool = False,
) -> TailResult:
    """N-slice sibling of ``write_review_trail``, for a ``decisions["review"]``
    list-shaped payload (partitioned-review fix, ``workstream_complete.
    build_write_trail_directives``) reaching the commit-tail path via
    ``wsc-tail.py``'s repeatable ``--review-slice`` flag -- the second of the
    two trail-writing paths that shape now needed to reach (the first,
    ``build_write_trail_directives`` itself, landed in ``a3d90f24dc16``).

    Calls ``write_review_trail`` once per qualifying slice -- never a single
    batched op call -- so ONE slice's foreign-session-range refusal or write
    failure can never suppress a sibling's write. This mirrors the directive
    path's own isolation (each ``d-write-trail-<index>`` dispatches through
    ``_execute_directives``'s per-directive halt contract independently), but
    reimplemented here as a plain sequential in-process loop: this call has
    no directive boundary to isolate at, and ``write_review_trail`` itself
    already never raises (every failure mode -- missing handler, a caught
    ``ForeignSessionRangeRefused``, any other exception -- collapses to a
    returned ``failed``/``failed_critical`` entry, not a raise), so a loop
    with no per-iteration try/except still cannot let one slice's failure
    abort the rest.

    A non-dict entry, or a dict missing/blanking one of
    ``_REVIEW_TRAIL_REQUIRED_FIELDS``, is silently dropped from
    ``qualifying`` before any write is attempted -- mirrors ``build_write_
    trail_directives``'s own "an incomplete entry contributes NO directive"
    convention (see that function's docstring) rather than forwarding a
    partial dict for ``write_review_trail`` to skip one-by-one. The CLI/argv
    assembly layer (``directives_commit_tail.build_close_tail_args_
    directive`` -> ``wsc-close.py tail-args`` -> ``wsc-tail.py``) is expected
    to have already filtered incomplete entries out before they ever become a
    ``--review-slice`` token; this is the same defensive posture one layer
    down, for a caller that invokes this op directly with an unfiltered list.

    ``partition_mandatory`` is ``write_review_trail``'s sibling gate, forwarded
    here unchanged -- see that function's own docstring paragraph.

    ``b_adjudication_present``'s breach check is evaluated ONCE across the
    whole list, never per slice: a reviewed close-out is in breach only when
    NOTHING usable was supplied at all (an empty/absent list, or every entry
    incomplete) -- surfacing N near-identical breach messages for N
    incomplete slices would be pure noise, and a caller that supplied at
    least one complete slice (regardless of whether that slice's own write
    later failed/was refused) has discharged the adjudication requirement;
    a genuine write failure on a qualifying slice still surfaces through that
    slice's own ``failed``/``failed_critical`` entry below, unaffected by
    this once-only gate.
    """
    qualifying = [
        entry
        for entry in (review_trail_list or [])
        if isinstance(entry, dict)
        and all(entry.get(k) not in (None, "") for k in _REVIEW_TRAIL_REQUIRED_FIELDS)
    ]

    if not qualifying:
        if b_adjudication_present or partition_mandatory:
            fields_str = ", ".join(_REVIEW_TRAIL_REQUIRED_FIELDS)
            reason = (
                "b_adjudication present" if b_adjudication_present else "partition_mandatory resolved"
            )
            msg = (
                f"{OP_REVIEW_TRAIL}: {reason} but review_trail (list) had no "
                f"complete entries -- required fields: {fields_str}"
            )
            return {"acted": [], "skipped": [], "failed": [], "failed_critical": [msg], "metadata_supplied": False}
        return {
            "acted": [], "skipped": [f"{OP_REVIEW_TRAIL}:no-review-metadata"], "failed": [],
            "metadata_supplied": False,
        }

    # Concurrent, not sequential (2026-08-15 measured fix, docs/plans/
    # 2026-07-22-wsc-tail-sub-2s-invoke-budget.md KPI regression): each
    # slice's own `write_review_trail` call is I/O-bound (a handful of
    # pathspec-scoped `git log`/`rev-parse` subprocess spawns per slice --
    # see `review_trail_write._guard_foreign_session_range` and
    # `_own_session_touched_paths_and_untrailered_flag`, each genuinely
    # re-derived per slice since every slice names a DIFFERENT sha_range and
    # the guard's per-call caches buy nothing across distinct ranges), and
    # `write_review_trail` itself already NEVER raises (every failure mode --
    # missing handler, a caught `ForeignSessionRangeRefused`, any other
    # exception -- collapses to a returned `failed`/`failed_critical` entry,
    # not a raise; see that function's own docstring), so `asyncio.gather`
    # with its default `return_exceptions=False` cannot let one slice's
    # failure suppress a sibling's -- the per-slice isolation property this
    # function's own docstring requires is unaffected, only the WALL CLOCK
    # changes: N slices' subprocess spawns now overlap (bounded by OS
    # process-creation throughput) instead of serializing N deep, which is
    # what measurably dominated cost at N=17 (measured: ~20s sequential on a
    # tiny synthetic repo, i.e. subprocess-spawn latency itself, not git's
    # own walk cost -- see this plan's own executor report / sidecar for the
    # measurement). `write_review_trail_entry`'s on-disk write path
    # (`_reserve_unique_trail_path`) is independently concurrency-safe by
    # construction (`os.O_CREAT | os.O_EXCL`, retried on `FileExistsError`),
    # so no new write-write race is introduced by running slices concurrently.
    # `return_exceptions=True` is load-bearing, not defensive noise. This
    # function's whole reason for calling per-slice rather than batching is
    # that one slice's refusal or failure must never suppress a sibling's
    # write. The default `gather(return_exceptions=False)` would propagate the
    # first exception and CANCEL the in-flight siblings, which is precisely the
    # invariant the docstring above promises -- and it would also make this
    # function raise, where `write_review_trail`'s own contract is that it
    # never does. Relying on that contract holding forever makes the isolation
    # a documentation claim rather than a structural property; a raising slice
    # is collapsed to a `failed_critical` entry below instead, so the guarantee
    # survives a future failure mode nobody has thought of yet.
    # C1 (docs/plans/2026-08-15-the-review-trail-write-stops-paying-n-wa.md):
    # precompute, ONCE for this whole batch, the identical-or-batchable
    # per-slice lookups review_trail_write.py's own module comment above
    # `build_batch_attribution_context` classifies as (a)/(b) -- the shared
    # is-inside-work-tree verdict, deliverable-id recovery value, and P2
    # attribution window/grep answer for every slice's single-commit
    # endpoint. `main_worktree_root(common_dir)` matches the exact root
    # `_review_trail_write_handler` itself derives from `repo_root` (never a
    # freshly re-derived one -- Anti-scope constraint 2). A context-build
    # failure degrades to `{}` (never raises -- see that function's own
    # docstring), which every consumer already treats as "no batched answer,
    # fall back to the original per-slice computation" -- this call can
    # never make write_review_trail_many less correct, only less batched.
    batch_context = _review_trail_write_mod.build_batch_attribution_context(
        main_worktree_root(common_dir),
        [entry["sha_range"] for entry in qualifying],
    )

    results_list = await asyncio.gather(
        *(
            write_review_trail(
                common_dir, entry, b_adjudication_present=False,
                _batch_context=batch_context,
            )
            for entry in qualifying
        ),
        return_exceptions=True,
    )

    acted: list = []
    skipped: list = []
    failed: list = []
    failed_critical: list = []
    for entry, result in zip(qualifying, results_list):
        if isinstance(result, BaseException):
            failed_critical.append(
                f"{OP_REVIEW_TRAIL}: slice {entry.get('sha_range')!r} raised "
                f"{type(result).__name__}: {result}"
            )
            continue
        acted.extend(result.get("acted", []))
        skipped.extend(result.get("skipped", []))
        failed.extend(result.get("failed", []))
        failed_critical.extend(result.get("failed_critical", []))

    # `qualifying` is non-empty here (the `if not qualifying` branch above
    # returned already), so at least one slice's metadata was complete --
    # metadata_supplied is True regardless of what each slice's own write
    # outcome was (mirrors write_review_trail's own "supplied != succeeded"
    # semantic -- see that function's docstring).
    out: TailResult = {"acted": acted, "skipped": skipped, "failed": failed, "metadata_supplied": True}
    if failed_critical:
        out["failed_critical"] = failed_critical
    return out


# ---------------------------------------------------------------------------
# cs_archive -- native port (session-dir move, idempotent)
# ---------------------------------------------------------------------------


def _sessions_dir(common_dir: Path) -> Path:
    """``<common_dir>/coordinator-sessions`` -- the Python-convention equivalent of the
    bash original's ``.git/coordinator-sessions`` (``common_dir`` IS ``<worktree>/.git``
    for the standard layout -- see ``fleet._common.main_worktree_root``)."""
    return common_dir / "coordinator-sessions"


def cs_archive(common_dir: Path, session_id: str) -> TailResult:
    """Native port of bash ``cs_archive <session_id>``.

    Moves ``<common_dir>/coordinator-sessions/<sid>`` to
    ``<common_dir>/coordinator-sessions/.archive/<sid>-<today>``. Idempotent: a missing
    session dir (already archived, or never existed) is a clean no-op, matching the bash
    original's ``return 0`` on absence -- never a failure.

    Always best-effort: any OSError during the move is captured into ``failed[]``, never
    raised (mirrors the bash original's ``|| return 1`` -- surfaced here as a soft failure
    rather than a hard exception so one archive failure cannot abort the rest of the tail).
    """
    sessions_dir = _sessions_dir(common_dir)
    sdir = sessions_dir / session_id
    if not sdir.is_dir():
        return {"acted": [], "skipped": [f"{OP_CS_ARCHIVE}:already-archived-or-absent"], "failed": []}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_dir = sessions_dir / ".archive" / f"{session_id}-{today}"

    try:
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sdir), str(archive_dir))
        return {"acted": [f"{OP_CS_ARCHIVE}:{session_id}"], "skipped": [], "failed": []}
    except OSError as exc:
        return _fail(OP_CS_ARCHIVE, f"{type(exc).__name__} -- {str(exc)[:160]}")


# ---------------------------------------------------------------------------
# cs_release_artifact -- native port (self-release, holder-identity-checked)
# ---------------------------------------------------------------------------


def _claim_held_by_me(claim_dir: Path, my_session_id: str) -> bool:
    """Return True iff ``claim_dir`` is currently held by ``my_session_id``.

    Keyed exclusively on the claim dir's ``session_id`` file -- NEVER on pid (see module
    negative-spec). A claim dir with no ``session_id`` file (legacy pid-only claim, or a
    dir that vanished mid-check) is treated as NOT held by me; it self-heals to
    ``session_id`` on the next takeover, same as the bash original's documented behavior.

    Re-reads the file fresh on every call -- calling this twice in sequence around a
    mutation IS the TOCTOU double-read discipline (see ``cs_release_artifact``).
    """
    if not my_session_id:
        return False
    session_id_file = claim_dir / "session_id"
    try:
        held_by = session_id_file.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"skip: _claim_held_by_me: held_by = session_id_file.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return bool(held_by) and held_by == my_session_id


def cs_release_artifact(common_dir: Path, artifact_class: str, basename: str) -> TailResult:
    """Native port of bash ``cs_release_artifact <class> <basename>``.

    Self-release ONLY: releases ``<common_dir>/coordinator-sessions/<class>-claims/<basename>``
    iff the CURRENT session is the recorded holder. Preserves the load-bearing TOCTOU
    double-read from the bash original (``_cs_claim_held_by_me`` called twice, keyed off
    ONE pre-resolved session id so the second read varies only on claim-dir CONTENT --
    the actual race -- never on a re-resolution of our own identity): a concurrent inline
    takeover (rm + mkdir + new holder) between the two reads is caught by the second read
    and this call becomes a clean no-op rather than deleting a live peer's claim.

    Always best-effort, always returns (never raises): missing claim dir, not-the-holder,
    and any OSError during removal are all clean no-ops / soft failures -- mirrors the bash
    original's unconditional ``return 0``.

    The F1 exit-verdict-ordering question (whether this call should even run, relative to
    the ceremony's success/failure) is decided by C9's orchestrator -- out of scope here.

    NEGATIVE SPEC -- own-repo ``common_dir`` ONLY. Holder identity is resolved from
    ``common_dir`` itself (``main_worktree_root(common_dir)`` -> ``resolve_current_session_id``),
    which is correct only for the wsc tail's own-repo case. A foreign-baton ``common_dir``
    (a claim held in a DIFFERENT repo than the one this session is running in) resolves no
    session id there and this call silently no-ops -- it never releases the foreign claim.
    Cross-repo / foreign-baton releases MUST route through
    ``coordinator_core.session.claims.release_artifact`` instead, which anchors holder
    identity to the running session's cwd rather than to ``common_dir``.
    """
    claim_dir = _sessions_dir(common_dir) / f"{artifact_class}-claims" / basename
    if not claim_dir.is_dir():
        return {"acted": [], "skipped": [f"{OP_CS_RELEASE_ARTIFACT}:already-absent"], "failed": []}

    worktree_root = main_worktree_root(common_dir)
    my_session_id = resolve_current_session_id(worktree_root) or ""

    # First read: am I the holder at all?
    if not _claim_held_by_me(claim_dir, my_session_id):
        return {"acted": [], "skipped": [f"{OP_CS_RELEASE_ARTIFACT}:not-holder"], "failed": []}

    # Second read (TOCTOU re-check): re-verify immediately before the destructive rm.
    # A takeover between the two reads flips this to False -- conservative outcome is to
    # skip, never delete a live peer's claim.
    if not _claim_held_by_me(claim_dir, my_session_id):
        return {"acted": [], "skipped": [f"{OP_CS_RELEASE_ARTIFACT}:holder-changed-toctou"], "failed": []}

    try:
        shutil.rmtree(claim_dir, ignore_errors=True)
        return {"acted": [f"{OP_CS_RELEASE_ARTIFACT}:{artifact_class}/{basename}"], "skipped": [], "failed": []}
    except OSError as exc:
        return _fail(OP_CS_RELEASE_ARTIFACT, f"{type(exc).__name__} -- {str(exc)[:160]}")
