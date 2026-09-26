"""
coordinator_core.ops.ceremony.tail_ops -- reused tail-op wiring + small cs_* native ports.

Purpose: in-process (no bash/node spawn) wiring of ceremony tail ops, plus native Python ports
of the two remaining ``cs_*`` bash functions the OLD ``wsc_commit.py`` shelled out to via
``_run_cs_function`` (``bash -c "source coordinator-session.sh && <fn>"``): ``cs_archive`` and
``cs_release_artifact``. Also carries ``refresh_roadmap_callout`` -- the disposable STEP_2_75
sibling render (native ``refresh_roadmap_callout.main`` port). The orchestrator
(``wsc_tail.py``) composes all of these helpers into the single-pass pipeline; this module
registers no top-level JSON-RPC op of its own.

Fleet-op wiring follows the confirm-then-act contract (T1 preview ``dry_run:true`` -> T3 act
``dry_run:false``), resolved by public op-key string via ``coordinator_core.ipc.get_op_handler``
rather than importing each op module's private handler function -- a future op-module refactor
that drops the public registration surfaces cleanly as a ``None`` return, not an
``AttributeError`` at a private import site.

The terminal-handoff sweep's live call site is
``commit_pipeline.run_commit_pipeline``'s ``_run_in_plane_archive_sweep`` -- in-process,
zero additional git spawns, zero additional commits. This module wires no call site for it;
``fleet.archive_completed_plans`` and ``fleet.archive_actioned_memos`` remain without any
occasioned call site here.

C5 (2026-07-23 wsc-tail-slim-down): ``refresh_roadmap_callout`` below was being dropped from
``wsc_tail.py``'s BLOCKING pre-commit tail and fired as a DETACHED CLI spawn instead, via
``fire_tracker_and_roadmap_detached``. This function was never wired into ``wsc_tail.py``
(unreached dead code, historically flagged as OPEN RESIDUE) and its tracker-CLI leg was
removed 2026-08-14 (``docs/plans/
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
      trail.write) and remains removed here even though the 2026-08-23 PM
      ruling later readmitted the op itself from suspension --
      ``coordinator_core/ops/review_trail_write.py`` is registered again
      (``coordinator_core/ops/__init__.py``), it was never deleted outright.
      This module still performs no call against it; a rebuild of this
      wiring is a separate decision from the op's own readmission. The
      cross-repo SEQUENTIAL PREDECESSOR contract this entry used to protect
      (DoE's chain-end ``review-coverage-gate.py``, SKILL.md:28, requiring
      ``VERDICT=COVERED`` before its own Step 3) is UNCOORDINATED by this
      module's continued non-wiring -- flagged to the EM, not resolved here;
      re-wiring a call site here must re-examine that contract first.
    - This module does NOT wire a terminal-handoff archival call site any more --
      ``fire_archive_sweeps_detached`` and ``_ARCHIVE_SWEEP_SCRIPTS`` were DELETED (C4,
      docs/plans/2026-08-25-the-terminal-handoff-sweep-stops-being-an-op.md § C4); a
      repo-wide grep for either name returns no live caller. The replacement in-plane
      call site lives in ``commit_pipeline.py``, not here.

REMOVED 2026-08-27 (PM ruling, abd587695): the in-plane archival sweep
`commit_pipeline._run_in_plane_archive_sweep` and its three legs are GONE from the
commit path. Text below describing it is retained only as history of why this code
looks the way it does -- it asserts nothing about the commit path today. Handoffs are
archived at the occasions that create the work (pickup, workstream-complete,
workday-complete, and the per-artifact lifecycle paths), never by sweeping a corpus on
commit. See state/kill-ledger.md.
"""

from __future__ import annotations
import sys

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

# "# noqa: F401 -- trigger registration" idiom used at every call site

_LOG = logging.getLogger(__name__)

TailResult = Dict[str, Any]

# Native-port op labels (not JSON-RPC op keys -- these never go through get_op_handler).
OP_CS_ARCHIVE = "cs_archive"
OP_CS_RELEASE_ARTIFACT = "cs_release_artifact"


def _empty_result() -> TailResult:
    return {"acted": [], "skipped": [], "failed": [], "unknown": []}


def _fail(op_label: str, reason: str) -> TailResult:
    return {"acted": [], "skipped": [], "failed": [f"{op_label}: {reason}"], "unknown": []}


def _ids(items: List[Any]) -> List[str]:
    return [
        (item.get("id") or item.get("path") or str(item)) if isinstance(item, dict) else str(item)
        for item in items
    ]


def fleet_result_to_tail(result: dict, op_label: str) -> TailResult:
    acted = _ids(result.get("acted", []))
    skipped = _ids(result.get("skipped", []))
    failed = _ids(result.get("failed", []))
    unknown = _ids(result.get("unknown", []))

    ec = result.get("exit_code", 0)
    if ec not in (0, 2) and not failed:
        failed.append(f"{op_label}: exit_code={ec}")

    return {"acted": acted, "skipped": skipped, "failed": failed, "unknown": unknown}


async def run_fleet_op_two_phase(
    handler_fn: Callable[..., Awaitable[dict]],
    op_label: str,
    common_dir: Path,
) -> TailResult:
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


# fire_archive_sweeps_detached and _ARCHIVE_SWEEP_SCRIPTS were DELETED here (C4,
# (STEP_2_75, C9 wiring-gap fix, 2026-07-22 -- see wsc_tail.py module docstring)

#: Native-port op label (not a JSON-RPC op key -- never goes through get_op_handler),
#: mirroring the OLD wsc_commit.py's ``_OP_ROADMAP_CALLOUT``.
OP_ROADMAP_CALLOUT = "node:refresh-roadmap-callout.sh"

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

    Loops over the caller-supplied ``consumed_handoff_paths`` list directly -- the
    orchestrator resolves the consumed set once, up front, via ``find_all_consumed_handoffs``,
    rather than threading it through a per-node ``PipelineContext`` read.

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
        return {
            "acted": [], "skipped": [f"{OP_ROADMAP_CALLOUT}:no-consumed-handoff"],
            "failed": [], "unknown": [],
        }

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
    if acted:
        stamp_liveness(str(worktree_root), _HL_ROADMAP_CALLOUT)

    return {
        "acted": acted, "skipped": skipped, "failed": failed, "unknown": [],
        "roadmap_stub_index_paths": stub_index_paths,
    }


# DETACHED CLI spawn rather than run in the BLOCKING wsc_tail.py pre-commit path.

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
    this function -- see module docstring). Mirrored the now-deleted
    `fire_archive_sweeps_detached`'s shape exactly (C2 precedent) -- same `spawn_detached`
    seam, same "record the SPAWN attempt only" result contract, no second spawn mechanism
    invented.

    Fires `refresh-roadmap-callout.py` once per distinct
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
    eventual render/rewrite/commit outcome -- the same contract the now-deleted
    `fire_archive_sweeps_detached` carried.

    Negative-spec:
        - Does NOT commit the CLI's output -- see "ARTIFACT-DISPOSITION RESIDUE"
          above. A caller relying on this function alone to close the dirty-tree-gate
          hazard the C2-era `extra_stage_paths` coupling used to close is NOT yet
          fully served; that gap must close before the pre-commit
          `refresh_roadmap_callout` call site is dropped.
        - Does NOT decide WHEN (relative to the ceremony's own commit) this function
          is called -- that is the caller's (`wsc_tail.py`'s) sequencing call. It MUST
          be called strictly AFTER the ceremony's own commit has landed, mirroring the
          post-commit call-site gating (`committed_sha is not None`) the now-deleted
          `fire_archive_sweeps_detached` used to have -- calling it pre-commit reopens
          the exact `.git/index.lock` contention / mid-write dirty-file hazard C2
          already hit once (module docstring "C2 correction").
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


def _sessions_dir(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions"


def cs_archive(common_dir: Path, session_id: str) -> TailResult:
    sessions_dir = _sessions_dir(common_dir)
    sdir = sessions_dir / session_id
    if not sdir.is_dir():
        return {
            "acted": [], "skipped": [], "failed": [],
            "unknown": [f"{OP_CS_ARCHIVE}:already-archived-or-absent"],
        }

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_dir = sessions_dir / ".archive" / f"{session_id}-{today}"

    try:
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sdir), str(archive_dir))
        return {"acted": [f"{OP_CS_ARCHIVE}:{session_id}"], "skipped": [], "failed": [], "unknown": []}
    except OSError as exc:
        return _fail(OP_CS_ARCHIVE, f"{type(exc).__name__} -- {str(exc)[:160]}")


def _claim_held_by_me(claim_dir: Path, my_session_id: str) -> bool:
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
        return {
            "acted": [], "skipped": [], "failed": [],
            "unknown": [f"{OP_CS_RELEASE_ARTIFACT}:already-absent"],
        }

    worktree_root = main_worktree_root(common_dir)
    my_session_id = resolve_current_session_id(worktree_root) or ""

    if not _claim_held_by_me(claim_dir, my_session_id):
        return {"acted": [], "skipped": [f"{OP_CS_RELEASE_ARTIFACT}:not-holder"], "failed": [], "unknown": []}

    if not _claim_held_by_me(claim_dir, my_session_id):
        return {
            "acted": [], "skipped": [], "failed": [],
            "unknown": [f"{OP_CS_RELEASE_ARTIFACT}:holder-changed-toctou"],
        }

    try:
        shutil.rmtree(claim_dir, ignore_errors=True)
        return {
            "acted": [f"{OP_CS_RELEASE_ARTIFACT}:{artifact_class}/{basename}"],
            "skipped": [], "failed": [], "unknown": [],
        }
    except OSError as exc:
        return _fail(OP_CS_RELEASE_ARTIFACT, f"{type(exc).__name__} -- {str(exc)[:160]}")
