"""
coordinator_core.ops.ceremony.tail_ops -- reused tail-op wiring + small cs_* native ports.

Purpose: in-process (no bash/node spawn) wiring of ceremony tail ops -- both
``coverage.gate`` and ``review_trail.write``'s in-process wiring were since removed
(see this module's own residue comments below) -- plus native Python ports of the two remaining
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
sweeps_detached`` replaced the three blocking calls with detached CLI fires, preserving the
whole budget win (detached ⇒ ~0ms blocking) while keeping every archival class occasioned at
every WSC pass. Accepted latency, stated honestly: archival now completes shortly AFTER the
ceremony returns, not within it -- the detached child races the parent's own exit, not its
commit. ``fleet.archive_completed_plans`` (and its ``sweep-terminal-plans.py`` CLI fire) was
killed and rebuilt from scratch 2026-08-23 (PM ruling) -- its detached fire is gone from
``_ARCHIVE_SWEEP_SCRIPTS`` until a rebuilt op re-earns a call site here.

``fleet.archive_actioned_memos`` (and its ``sweep-actioned-memos.py`` CLI fire) was killed
outright the same day (PM ruling, no replacement op) -- the memo family has no detached fire
here any more; only the shipped-handoffs sweep remains in ``_ARCHIVE_SWEEP_SCRIPTS``.

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
    - ``review_trail.write``'s in-process wiring (formerly this module's
      ``write_review_trail`` / ``write_review_trail_many`` / ``review_trail_
      metadata_complete``) was removed 2026-08-23 (PM ruling, kill review_
      trail.write) along with the op itself. The cross-repo SEQUENTIAL
      PREDECESSOR contract this entry used to protect (DoE's chain-end
      ``review-coverage-gate.py``, SKILL.md:28, requiring ``VERDICT=COVERED``
      before its own Step 3) is UNCOORDINATED by this removal -- flagged to
      the EM, not resolved here; a rebuilt op must re-examine that contract
      before this module (or any successor) re-wires a write path against it.
    - ``fire_archive_sweeps_detached`` does NOT fire the composite ``coordinator/bin/
      sweep-boot.py`` -- that composite also runs the unintegrated-findings reap (a tracked
      ``git rm``), a destructive sweep out of scope for a call fired on every WSC pass
      (plan § C2). It fires the per-class CLIs directly instead.
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
#
# coverage.gate (coordinator_core.ops.coverage_gate) is deliberately NOT
# pre-imported here any more (K-001, state/kill-ledger.md): the close path no
# longer calls this op in-process -- see the retired `run_coverage_gate`
# below this module's own history. The op still exists as mint-only
# plumbing reachable from `cmd_brightline_gate` (removed, K-007)
# (coordinator/bin/wsc-coverage-gate-runner.py), which imports and registers
# it in its own process.
#
# review_trail_write (coordinator_core.ops.review_trail_write) is likewise no
# longer pre-imported here -- the op it registered, review_trail.write, was
# deleted outright (PM ruling 2026-08-23, kill review_trail.write); see the
# "review_trail.write" residue comment below for what this module used to
# wire against it.

_LOG = logging.getLogger(__name__)

TailResult = Dict[str, Any]

# Results-dict label for the single detached-fire entry (C2) that replaced the three
# retired blocking OP_ARCHIVE_PLANS / OP_ARCHIVE_HANDOFFS / OP_ARCHIVE_MEMOS wrappers --
# see `fire_archive_sweeps_detached` and the module docstring's "Archive-sweeps are
# DETACHED" section.
OP_ARCHIVE_SWEEPS_DETACHED = "archive_sweeps:detached_fire"

# Native-port op labels (not JSON-RPC op keys -- these never go through get_op_handler).
OP_CS_ARCHIVE = "cs_archive"
OP_CS_RELEASE_ARTIFACT = "cs_release_artifact"


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
# `housekeeping_liveness.REMEDY_COMMANDS[ARCHIVE_SWEEPS]` (same scripts) -- NOT the
# composite `sweep-boot.py` (module docstring negative-spec; plan § C2 anti-scope).
_ARCHIVE_SWEEP_SCRIPTS: tuple = (
    "sweep-shipped-handoffs.py",   # fleet.archive_completed_handoffs, deployment_state branch (shipped/abandoned/superseded)
)


def fire_archive_sweeps_detached(worktree_root: Path) -> TailResult:
    """Fire the per-class archive-sweep CLIs detached (C2) -- the replacement for the
    retired blocking calls (``archive_completed_handoffs`` / ``sweep_actioned_memos``,
    both removed). The other two former members of ``_ARCHIVE_SWEEP_SCRIPTS`` are also
    gone: ``archive_completed_plans`` was killed and rebuilt from scratch 2026-08-23
    (its ``sweep-terminal-plans.py`` fire is gone until a rebuilt op re-earns a call
    site here); ``fleet.archive_actioned_memos`` was killed outright the same day (PM
    ruling) with no replacement op yet, so its ``sweep-actioned-memos.py`` fire is gone
    too.

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
# coverage.gate's ceremony-close wiring was removed here (K-001,
# state/kill-ledger.md): the DAG fixpoint walk it drove cost ~150-180s per
# close, one git subprocess per chain commit. `coordinator_core.coverage.
# run_coverage_gate`, the `coverage.gate` op handler, and
# `coordinator/bin/review-coverage-gate.py` still exist as mint-only
# plumbing reachable from `cmd_brightline_gate` (removed, K-007)
# (coordinator/bin/wsc-coverage-gate-runner.py) -- see that op module's own
# docstring for the live kill-candidate note.
# ---------------------------------------------------------------------------
# review_trail.write's in-process wiring (review_trail_metadata_complete,
# write_review_trail, write_review_trail_many) was removed here (PM ruling
# 2026-08-23, kill review_trail.write): the op itself
# (coordinator_core/ops/review_trail_write.py) was deleted, kill-means-kill,
# no successor built yet. This module registers no top-level JSON-RPC op of
# its own and performed no direct write for review-trail -- every write site
# lived in the deleted op module, so nothing here needed a replacement write
# path, only removal of the dead call.
# ---------------------------------------------------------------------------


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
