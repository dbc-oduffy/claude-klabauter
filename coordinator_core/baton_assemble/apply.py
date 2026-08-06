"""
coordinator_core.baton_assemble.apply -- the `baton-assemble apply` computed-
skill engine: the MUTATING half `brief()`'s read-only decision object hands
off to.

Purpose: recomputes the brief in-process from `(kind, artifact_path)` (never
trusts a caller-supplied decision object), then executes the brief's
`directives[]` through a CLOSED, literal dispatch table -- CONSUMING
`coordinator_core.contract.apply_base` for the directive-execution engine,
dependency ordering, CLI-resolution seam, session-identity propagation, and
scoped-commit discipline (the second real apply/dispatch half named by DR-092
as the un-defer trigger; `apply_base` already ships, this module composes it
from the start rather than mirroring pickup_assemble's own now-superseded
local copy -- see `coordinator_core/contract/apply_base.py`'s own module
docstring for the C2 amendment history).

This module keeps only what is genuinely baton-specific: its own closed
`_CLI_DISPATCH` table, the handler bodies binding each directive `cli` name
to the existing atomic CLI/op it names, `_run_git` (the in-process git
read-model), and the `apply()` orchestration (brief-recompute, session
identity, scoped commit).

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md,
chunk C1

RESUME MODEL (2026-07-29 break-class fix): there is no `--continue`/`--resume`
flag and no persisted run-state file. `apply_base.execute_directives` has no
rollback -- a raised handler returns `APPLY_EXIT_PARTIAL_MUTATION` with whatever
already landed, plus d1's compensator pass -- so the sanctioned resume path is
RE-RUNNING THE IDENTICAL COMMAND. `apply()` already recomputes `brief()` from
`(kind, artifact_path)` on every call, and `brief()` now derives each directive's
`already_satisfied` from on-disk state (see `coordinator_core/baton_assemble/
__init__.py`'s `_resume_recorded_successor_path` and `_build_directives`'s d1
block), so the second run IS the continuation. A run-state file was considered
and rejected: it would be a second source of truth for facts the artifacts
already carry, with its own staleness question and its own thing for an operator
to remember. Live on 2026-07-29 the absent resume left the operator hand-running
d2/d5/d6 one directive at a time -- the "relocated transcription" the project's
own north-star discharge test forbids.

PRISTINE-SCAFFOLD PREDICATE (2026-07-30): two sites here decide whether a
successor file on disk may be DELETED -- `_compensate_d1_scaffold` (after a
partial abort) and d6's own inline `_cleanup_successor`. Both consult the one
predicate, `_is_pristine_generator_scaffold`, which answers "does this file carry
anything the scaffold generator did not write" by re-rendering
`coordinator-doc-new`'s own template through
`coordinator-doc-new`'s own scaffolder function and comparing bytes. It composes
the generator rather than re-deriving what a template looks like, records
nothing, and fails SAFE in one direction only: every uncertainty declines to
delete.

Negative-spec:
    - Do NOT add a `--continue`/`--resume` flag or a persisted run-state file --
      re-running the identical invocation is the resume path, by design (see
      RESUME MODEL above).
    - Do NOT hand-roll a second answer to "is this file a pristine scaffold" --
      `_is_pristine_generator_scaffold` is the one home, and it works by CALLING
      the generator. A local placeholder-marker/heuristic check beside it is the
      exact shape the 2026-07-30 fix retired.
    - Do NOT derive a directive's satisfaction predicate in THIS module -- every
      one is computed once, at `brief()` time, by `_build_directives`; this
      module only reports what it was handed (`_collect_replayed_directives`).
    - Do NOT add a dispatch entry that resolves `cli` via `getattr`,
      `importlib`, or any brief-derived string -- every entry in
      `_CLI_DISPATCH` is a literal key written by hand in this file.
    - Do NOT re-derive `apply_base`'s directive-ordering/judgment-gating/
      scoped-commit logic locally -- consume it (anti-scope: never edit
      `coordinator_core/contract/apply_base.py` from this module).
    - Do NOT trust a caller-supplied decision object as the mutation plan --
      `apply()` recomputes `brief()` itself from `(kind, artifact_path)`.
    - Do NOT stage or commit with `git add -A`/`git add .`/a bare `git
      commit` -- every git call names its one resolved artifact path as an
      explicit pathspec (via `apply_base.scoped_commit`).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.contract import apply_base
from coordinator_core.contract.apply_base import (
    APPLY_EXIT_OK,
    APPLY_EXIT_TRANSPORT_FAIL,
    UnrecognizedDirective,
)
from coordinator_core.baton_assemble import validate_decisions_shape
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)

# Import side-effect only: triggers each op module's register_op(...) so
# _invoke_op_in_process's get_op_handler() lookups below resolve via a direct
# registry hit rather than its lazy-import fallback -- mirrors the established
# pattern in ops/ceremony/consumed_handoff_stamp.py, ops/ceremony/wsc_tail.py,
# ops/cutover_advance.py, et al. Originally added
# to fix a live break (get_op_handler() alone, with no import trigger, returned
# None for an op whose owning module was never otherwise imported in this
# process -- `{"error": "unrecognized op 'handoff.stamp_phase'", "failed_directive":
# "d3", ...}` from _dispatch_handoff_stamp_phase); get_op_handler() self-resolves
# a MISS since 2026-07-25 (see coordinator_core/ipc.py), so this pre-import is now
# belt-and-braces, not strictly required for correctness. handoff.stamp_phase's
# own directive is no longer emitted by _build_directives (see
# coordinator_core/baton_assemble/__init__.py's module docstring), but the
# dispatch-table entry below stays correct/dispatchable -- import kept for
# hygiene, not dead weight left silently broken.
import coordinator_core.ops.handoff_author_fork  # noqa: F401
import coordinator_core.ops.handoff_phase_stamp  # noqa: F401
import coordinator_core.ops.handoff_archive_transition  # noqa: F401

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_SESSION_ENV_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID")
_SESSION_ENV_READ_ORDER = (
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, **_NO_CONSOLE
    )


# ---------------------------------------------------------------------------
# Directive handlers -- each binds a `directives[].cli` name to the ONE
# existing atomic CLI/op it names. Every handler receives `(args, repo_root)`
# and returns a detail dict; a raised exception is caught by
# `apply_base.execute_directives` and surfaced as `APPLY_EXIT_PARTIAL_MUTATION`.
# ---------------------------------------------------------------------------


def _dispatch_coordinator_doc_new(args: list[str], repo_root: Path) -> dict[str, Any]:
    from coordinator_core.resolution.facade import resolve_operator_config

    claude_klabauter_bin = resolve_operator_config()["claude_klabauter_bin"]
    cli = str(Path(claude_klabauter_bin) / "coordinator-doc-new")
    proc = subprocess.run(
        [sys.executable, cli, *args], cwd=repo_root, capture_output=True, text=True, **_NO_CONSOLE
    )
    if proc.returncode != 0:
        raise RuntimeError(f"coordinator-doc-new {args}: failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return {"cli": "coordinator-doc-new", "args": args, "stdout": proc.stdout.strip()}


def _dispatch_lint_frontmatter(args: list[str], repo_root: Path) -> dict[str, Any]:
    from coordinator_core.resolution.facade import resolve_operator_config

    claude_klabauter_bin = resolve_operator_config()["claude_klabauter_bin"]
    cli = str(Path(claude_klabauter_bin) / "lint-frontmatter.py")
    proc = subprocess.run(
        [sys.executable, cli, *args], cwd=repo_root, capture_output=True, text=True, **_NO_CONSOLE
    )
    if proc.returncode != 0:
        raise RuntimeError(f"lint-frontmatter {args}: failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return {"cli": "lint-frontmatter", "args": args}


def _dispatch_session_claim_cli(args: list[str], repo_root: Path) -> dict[str, Any]:
    from coordinator_core.resolution.facade import resolve_operator_config

    claude_klabauter_bin = resolve_operator_config()["claude_klabauter_bin"]
    cli = str(Path(claude_klabauter_bin) / "session-claim-cli")
    proc = subprocess.run(
        [sys.executable, cli, *args], cwd=repo_root, capture_output=True, text=True, **_NO_CONSOLE
    )
    if proc.returncode != 0:
        raise RuntimeError(f"session-claim-cli {args}: failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return {"cli": "session-claim-cli", "args": args}


def _invoke_op_in_process(op_name: str, params: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """Invokes a registered `coordinator_core.ipc` op handler directly
    in-process (no UDS hop) -- used for directives naming an op rather than
    a standalone `bin/` CLI (e.g. `handoff.stamp_phase`, `handoff.author_fork`).

    `repo_root` arrives here as the resolved WORKTREE root (`resolve_repo_root()`
    runs `git rev-parse --show-toplevel` -- see `apply()` below), but ops are
    keyed per `coordinator_core.ipc.OP_KEY_SCOPE`/`_OP_KEY_SCOPE` (the SAME table
    `ipc.py`'s own `resolve_op_repo_key` consults for the UDS transport path) --
    reused here rather than re-derived, so this in-process shortcut never drifts
    from the transport path's op-scope classification. A `"common_dir"`-scoped op
    (e.g. `handoff.stamp_phase`, `handoff.author_fork`) MUST receive
    `git_common_dir(worktree_root)`, mirroring `ipc.py`'s own conversion --
    handing it the raw worktree root instead lands one directory ABOVE the repo
    once the handler takes `.parent` (main_worktree_root's documented contract),
    which is the exact defect this conversion fixes for
    `handoff.author_fork`/`handoff.stamp_phase`. A `"show_top"`-scoped op gets the
    worktree root unconverted; a `"none"`-scoped op gets `None`, matching
    `resolve_op_repo_key`'s own "no repo state accessed" contract.
    """
    from coordinator_core.ipc import get_op_handler, OP_KEY_SCOPE
    from coordinator_core.lifecycle import git_common_dir

    handler = get_op_handler(op_name)
    if handler is None:
        raise UnrecognizedDirective(f"unrecognized op {op_name!r}")

    scope = OP_KEY_SCOPE.get(op_name, "none")
    if scope == "common_dir":
        op_repo_root: Optional[Path] = git_common_dir(repo_root)
    elif scope == "show_top":
        op_repo_root = repo_root
    else:
        op_repo_root = None

    return asyncio.run(handler(params, op_repo_root))


def _dispatch_handoff_stamp_phase(args: list[str], repo_root: Path) -> dict[str, Any]:
    """Stamps `handoff_phase` onto an existing handoff via the
    `handoff.stamp_phase` op.

    FAIL POSTURE (key on the op's exit_code -- and note this is the OPPOSITE
    call from `_dispatch_handoff_supersede_predecessor` below, deliberately):
    the right predicate is per-op, not a house rule. `handoff.stamp_phase`
    returns `exit_code:1` for every one of its own `_err(...)` paths
    (missing/invalid param, path escaping `state/handoffs/`, handoff not found
    on disk, lock timeout, read/write failure) and `exit_code:0` for both a
    real write and an already-converged byte-identical no-op -- so exit_code IS
    the failure signal here, and `applied` is NOT: `applied:False` at
    `exit_code:0` is a legitimate converged no-op, and raising on it would fail
    every re-run of an already-stamped handoff. `supersede_predecessor` reads
    `superseded` instead precisely because ITS op returns `exit_code:0` on a
    graceful retain, which exit_code cannot distinguish from success.

    Without this raise the handler swallowed failure the same way
    `_dispatch_handoff_author_fork` did before its third-generation fix
    documented below: `_invoke_op_in_process` returns the op's error dict as an
    ordinary value, and `apply_base.execute_directives` treats only a RAISED
    exception as failure, so a failed stamp would land in `landed` under a
    green `APPLY_EXIT_OK`. Latent when written (`_build_directives` does not
    emit this directive for `kind=handoff` -- d1's scaffold stamps
    `handoff_phase:continuation` itself), but the handler stays registered in
    `_CLI_DISPATCH`, one emission decision away from live. Reported by
    example-doctrine-repo-em, 2026-07-29 cross-repo memo."""
    artifact_path = args[0] if args else ""
    result = _invoke_op_in_process(
        "handoff.stamp_phase", {"handoff_path": artifact_path}, repo_root
    )
    if result.get("exit_code") != 0:
        raise RuntimeError(
            "handoff.stamp_phase failed to stamp handoff_phase onto "
            f"{artifact_path!r} (exit_code={result.get('exit_code')!r}, "
            f"applied={result.get('applied')!r}, error={result.get('error')!r}) "
            "-- aborting rather than reporting a green apply() over an "
            "unstamped handoff"
        )
    return {"cli": "handoff.stamp_phase", "args": args, "result": result}


def _dispatch_handoff_author_fork(args: list[str], repo_root: Path) -> dict[str, Any]:
    """kind=spinoff's d3 directive: STAMPS the five origin_* provenance fields
    onto d1's already-minted readable-slug artifact, via `handoff.author_fork`'s
    stamping-mode contract (`params["handoff_path"]` present -- see that op's
    module docstring "Stamping mode" section). d3 does NOT author a second
    file -- that was the bug this rewrite closes (Option A, ratified
    2026-07-27): d1 (`coordinator-doc-new --type=spinoff`) mints the ONE file
    the operator keeps; it previously carried zero origin_* provenance while a
    second, orphaned file d3 authored from scratch carried the correct
    provenance and was never read by anything downstream.

    args (6 slots, positional, per `_build_directives`'s d3 emission):
        [origin_handoff, origin_handoff_id, origin_session, origin_plan_id,
         origin_goal_id, handoff_path]
    `handoff_path` is d1's own `lineage["output_path"]` -- the fresh path d1
    scaffolded THIS run, i.e. the stamp target. `origin_goal_id` arrives as a
    single ';'-joined string (JSON/list values do not survive a flat `args:
    list[str]` directive shape) and is split back into a list here; empty
    string means "no goal ids" (op self-resolves).

    Previous defects fixed here (both were silent -- neither errored, both
    corrupted provenance): `origin_handoff` used to get bound into `title`
    (the from-scratch author path's field) instead of passed through as
    `origin_handoff`; `origin_session` was unpacked into a local and then
    NEVER placed into `params` at all, so the caller-supplied session was
    silently discarded and the op always fell back to self-resolving its own
    live session id instead.

    THIRD-GENERATION defect fixed here (2026-07-27 follow-up,
    author-fork-seam-repair spinoff): a stamp-mode failure from the op
    (e.g. `handoff_path not found on disk`, `escapes state/handoffs/`, a
    lock timeout, an I/O error -- any of `_handle_stamp`'s `_err(...)`
    returns) used to be swallowed here. `_invoke_op_in_process` returns
    the op's error dict as an ordinary return value, never an exception,
    so a failed stamp result flowed straight back to
    `apply_base.execute_directives` as `detail` -- which only treats a
    RAISED exception as failure (see its own docstring/loop: `results.append(...)`
    and `landed.append(directive_id)` run unconditionally after a
    non-raising `handler(...)` call). The whole `apply()` run then reported
    `APPLY_EXIT_OK` and a `commit_sha`, with the target handoff carrying
    ZERO `origin_*` fields and no visible signal anywhere. This mirrors
    `_dispatch_handoff_supersede_predecessor`'s own FAIL POSTURE above
    (`superseded is False` raises `RuntimeError`) -- same convention
    applied here: a stamp result whose `status` is not `"ok"` raises
    instead of returning normally, so `apply_base.execute_directives`
    reports `APPLY_EXIT_PARTIAL_MUTATION` and the failure surfaces to the
    operator rather than hiding behind a green `apply()` exit."""
    if len(args) != 6:
        raise ValueError(
            f"_dispatch_handoff_author_fork expects exactly 6 positional args "
            f"(origin_handoff, origin_handoff_id, origin_session, origin_plan_id, "
            f"origin_goal_id, handoff_path) but got {len(args)}: {args!r} -- "
            "_build_directives's d3 emission and this unpack must change together"
        )
    (
        origin_handoff,
        origin_handoff_id,
        origin_session,
        origin_plan_id,
        origin_goal_id_joined,
        handoff_path,
    ) = args
    origin_goal_id = [g for g in origin_goal_id_joined.split(";") if g] or None
    params = {
        "handoff_path": handoff_path,
        "origin_handoff": origin_handoff or None,
        "origin_handoff_id": origin_handoff_id or None,
        "origin_session": origin_session or None,
        "origin_plan_id": origin_plan_id or None,
        "origin_goal_id": origin_goal_id,
    }
    result = _invoke_op_in_process("handoff.author_fork", params, repo_root)
    if result.get("status") != "ok":
        raise RuntimeError(
            "handoff.author_fork (stamp mode) failed to stamp origin_* "
            f"provenance onto {handoff_path!r} (status={result.get('status')!r}, "
            f"exit_code={result.get('exit_code')!r}, error={result.get('error')!r}) "
            "-- aborting this mint rather than reporting success with unstamped "
            "provenance"
        )
    degraded = result.get("degraded")
    if degraded:
        # A degrade still reports status:"ok" (see _handle_stamp's own
        # docstring -- ambiguity in stamp mode DEGRADES to null rather than
        # aborting), so the RuntimeError branch above never fires for it.
        # Left at that, the only trace was `degraded` nested two levels
        # inside `result["result"]` -- invisible to any operator not reading
        # the full JSON report by hand. Surfaced at top level of THIS
        # directive's own returned dict (same place `cli`/`args`/`result`
        # already live, so `report["results"][i]["detail"]["degraded"]` is a
        # direct read, not a nested dig) and printed to stderr, matching
        # this module's existing stderr-for-non-fatal-advisory convention
        # (`_usage`, the malformed-`--decisions`-JSON and unrecognized-
        # argument prints in `main_apply`). Review: code-reviewer (P1) --
        # `degraded` had no non-test consumer anywhere in the tree.
        # Review: code-reviewer -- the engine's own `reason` string (which
        # already distinguishes `below-threshold` "nothing scored high
        # enough" from `too-close` "a genuine tie", per
        # match_core.ResolutionReason) is surfaced directly rather than
        # re-derived here as a re-asserted "ambiguous match" label -- the
        # printer must not claim a judgment call happened when it didn't.
        print(
            f"baton-assemble apply: handoff.author_fork (stamp mode) degraded "
            f"{len(degraded)} field(s) to null on {handoff_path!r} -- "
            + "; ".join(f"{d.get('field')}: {d.get('reason')}" for d in degraded)
            + " (disambiguation candidates in the full report)",
            file=sys.stderr,
        )
    return {
        "cli": "handoff.author_fork",
        "args": args,
        "result": result,
        "degraded": degraded or None,
    }


def _dispatch_render_project_tracker(args: list[str], repo_root: Path) -> dict[str, Any]:
    """kind=handoff's d4: re-renders `docs/project-tracker.md`. Dispatches
    the standalone `coordinator/bin/render-project-tracker` subprocess CLI
    -- the SAME shape as `_dispatch_coordinator_doc_new` /
    `_dispatch_lint_frontmatter` / `_dispatch_session_claim_cli` above --
    NOT `_invoke_op_in_process`. `render-project-tracker` self-resolves its
    own `store_root`/`coordinator_root_path` from `cwd` (git rev-parse) and
    this script's own on-disk git anchor; it takes no argv of its own, so
    `args` is always `[]` here and passed through only for report-shape
    parity with the other subprocess dispatchers.

    2026-07-28 break-class fix (bug reproduced live: `baton-assemble apply
    handoff <slug>` landed d1/d2 then hard-aborted d4 with
    `{"error": "unrecognized op 'project.render_tracker'"}`): the previous
    body called `_invoke_op_in_process("project.render_tracker", ...)`, but
    no module anywhere registers an op by that name -- `render-project-
    tracker` was never an `ipc` op at all, it is (and always was) a
    standalone `coordinator/bin/` CLI, reached by subprocess exactly like
    d1's `coordinator-doc-new` or d2's `lint-frontmatter`. The capability
    genuinely exists (confirmed: `coordinator/bin/render-project-tracker`
    is the SOLE writer of `docs/project-tracker.md`, per that script's own
    module docstring) -- this was a wiring defect (wrong dispatch shape),
    not a missing capability, so the fix redirects the call rather than
    inventing a renderer or degrading the directive to a no-op.

    FAIL POSTURE (2026-07-29 break-class fix) -- this handler NEVER raises on
    a renderer non-zero exit; it DEGRADES. That is the opposite posture from
    d3's and d6's, and the difference is what the directive writes:
    `docs/project-tracker.md` is a DERIVED VIEW, rendered 1:1 from
    `state/workstreams/` and re-renderable at any later moment from the same
    inputs, and no directive in this envelope depends on d4. The mint is the
    load-bearing artifact. Raising here reported APPLY_EXIT_PARTIAL_MUTATION,
    which (a) fired `_D1_COMPENSATORS` and DELETED the freshly-scaffolded
    successor, and (b) aborted d5/d6 before they ran -- trading the operator's
    entire save-state for a stale copy of a regenerable view.

    Live repro (claude-klabauter, 2026-07-29): every repo whose tracker is
    hand-curated and whose `state/workstreams/` is empty -- i.e. every
    consumer repo that never adopted the workstream queue -- hit the
    renderer's zero-workstream truncation guard, so `/handoff` could not mint
    a baton AT ALL there. The guard was right to decline (it exists to stop a
    157-line curated tracker becoming an 18-line stub); the defect was this
    seam treating "correctly declined" as a transaction-fatal error, and then
    leaving the operator to hand-run d2/d5/d6 one directive at a time.

    `EXIT_NOT_APPLICABLE` is read here rather than string-matching stderr, so
    "not queue-backed" is distinguishable in the report from a genuine
    renderer fault on the FAULT axis alone. That collapsed a second, distinct
    DATA axis: a non-zero, non-EXIT_NOT_APPLICABLE exit could mean either "the
    renderer itself broke" or "the renderer ran fine but the truncation guard
    fired because the input data collapsed a previously-populated tracker to
    zero content" -- two different things an operator would chase completely
    differently. The mapping is therefore three-way, keyed on exit code:
    `EXIT_NOT_APPLICABLE` -> "tracker-not-queue-backed" (hand-curated repo,
    nothing to render, benign); `EXIT_RENDER_REGRESSION` -> "tracker-render-
    regression" (the renderer ran, but its OWN truncation guard caught a
    collapse-to-zero over a tracker that previously had content -- the
    SUSPECT is the input data, not this dispatcher); anything else ->
    "render-failed" (a genuine renderer fault). All three degrade identically
    in the RETURNED dict -- none of them raise, and `reason` stays populated
    for every one of the three so a `--json` consumer can always tell "rendered
    fine" from "not applicable" from "regressed" from "broke" without parsing
    prose. Do NOT restore the raise for any of these cases: a broken renderer
    or a regressed render is still not worth destroying a baton over.

    STDERR PRINTING IS NOT THE SAME QUESTION (2026-08-05 fix). Every consumer
    repo whose tracker is hand-curated by design -- carries no information,
    never resolves to anything else -- hit `tracker-not-queue-backed` on
    EVERY SINGLE `/handoff`, so this print fired every single time and taught
    its reader to stop reading it, including the one below it that shares this
    stderr stream and genuinely matters (`tracker-render-regression`, the
    renderer's truncation guard catching a collapse over a previously-
    populated tracker -- a suspected bug, not a steady state). A warning that
    always fires is not a warning. `tracker-not-queue-backed` is therefore
    demoted to a quiet, structured-only outcome: still `degraded` in the
    returned dict (nothing about the information is dropped, only its
    permanently-firing stderr shout), never printed. `tracker-render-
    regression` and `render-failed` are unaffected -- both keep printing
    exactly as before, because both name something worth an operator's
    attention on a stream they can otherwise trust.
    """
    from coordinator_core.resolution.facade import resolve_operator_config
    from coordinator_core.ops.render_project_tracker import (
        EXIT_NOT_APPLICABLE,
        EXIT_RENDER_REGRESSION,
    )

    claude_klabauter_bin = resolve_operator_config()["claude_klabauter_bin"]
    cli = str(Path(claude_klabauter_bin) / "render-project-tracker")
    proc = subprocess.run(
        [sys.executable, cli, *args], cwd=repo_root, capture_output=True, text=True, **_NO_CONSOLE
    )
    degraded = None
    if proc.returncode != 0:
        if proc.returncode == EXIT_NOT_APPLICABLE:
            reason = "tracker-not-queue-backed"
        elif proc.returncode == EXIT_RENDER_REGRESSION:
            reason = "tracker-render-regression"
        else:
            reason = "render-failed"
        degraded = {
            "reason": reason,
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
        }
        if reason == "tracker-not-queue-backed":
            message = (
                "this repo's docs/project-tracker.md is hand-curated and its "
                "state/workstreams/ store is empty, so there is nothing to "
                "render; the tracker was left untouched"
                f" (renderer stderr: {proc.stderr.strip()})"
            )
        elif reason == "tracker-render-regression":
            message = (
                "the render collapsed docs/project-tracker.md to zero content "
                "over a tracker that previously had content -- the renderer's "
                "own truncation guard caught this and declined to write; the "
                f"SUSPECT is the input data, not the renderer (rc={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        else:
            message = f"the renderer failed (rc={proc.returncode}): {proc.stderr.strip()}"
        if reason != "tracker-not-queue-backed":
            # `tracker-not-queue-backed` stays silent on stderr -- see this
            # handler's own docstring, STDERR PRINTING IS NOT THE SAME
            # QUESTION. It is still fully represented in `degraded` below, so
            # a `--json` consumer loses nothing; only the permanently-firing
            # shout is gone.
            print(
                "baton-assemble apply: render-project-tracker degraded -- "
                + message
                + ". docs/project-tracker.md is a derived view -- the baton mint "
                "and every other directive in this run proceeded normally.",
                file=sys.stderr,
            )
    return {
        "cli": "render-project-tracker",
        "args": args,
        "stdout": proc.stdout.strip(),
        "degraded": degraded,
    }


def _dispatch_handoff_supersede_predecessor(args: list[str], repo_root: Path) -> dict[str, Any]:
    """kind=handoff's d6 (2026-07-27, computed-skills-b4 plan C1 -- the
    push-side succession writer): the fix for a continuation baton's
    predecessor being left non-terminal forever. Composes the existing op
    `handoff.archive_transition` mode="supersede" -- stamps the
    PREDECESSOR status:claimed + deployment_state:continued +
    continued_into:<this successor> and archives it (git mv to
    archive/handoffs/YYYY-MM/ + commit), all in ONE call, once the guard
    clears.

    args: [predecessor_path, continued_into, exclude_path] -- `continued_into`
    and `exclude_path` are the SAME value (this successor's own already-
    normalized artifact_path, per `_build_directives`'s d6 emission).
    `exclude` is REQUIRED, not optional: the live-children guard's default
    edge kinds include `predecessor` over a live set spanning state/ AND
    archive/, and the successor names its own predecessor via the
    `predecessor:` field it was scaffolded with -- omitting `exclude` makes
    the guard see the freshly-minted successor as the predecessor's OWN
    live child and "retain gracefully", silently no-opping the entire
    point of this directive.

    FAIL POSTURE (do not key on the op's exit_code): the op returns
    exit_code:0 even on a graceful retain -- an unrelated live child, or an
    internal ownership-guard refusal during the shipped_in derivation --
    which is indistinguishable from "stamped and archived" by exit_code
    alone. This handler reads the op's own `superseded: bool` return
    instead. `superseded is False` raises, which
    `apply_base.execute_directives` reports as
    APPLY_EXIT_PARTIAL_MUTATION -- a half-applied succession (successor
    minted, predecessor left un-superseded) is the EXACT stranding defect
    this directive exists to eliminate, so it must never silently succeed.
    Also removes the successor artifact d1 already scaffolded earlier in
    THIS SAME run before raising -- narrowed 2026-07-30 to a successor that
    is still a PRISTINE generator scaffold, see `_cleanup_successor`'s own
    docstring for why an unconditional unlink here was break-class once a
    replay could hand this handler a file d1 did not write. This cleanup
    must stay here, inline,
    rather than deferring to `apply_base.execute_directives`'s own
    `compensators` seam (`_D1_COMPENSATORS`/`_compensate_d1_scaffold`
    above): that seam only runs AFTER a directive raises and
    `execute_directives` catches it, but THIS handler is the one doing the
    raising, so its own cleanup must run first, before the exception ever
    reaches that catch. (In practice `_compensate_d1_scaffold` would find
    nothing left to do here regardless -- this handler already deletes the
    file -- but that is incidental, not the reason this call stays inline.)
    A stranded, uncommitted successor file left on disk is itself the
    failure mode being guarded against here.

    Open upstream seam (do not resolve here): kind=spinoff's own
    artifact-authoring directive still dispatches to `handoff.author_fork`,
    which stamps `predecessor: none` on a fork's origin (see
    ops/handoff_author_fork.py:~212) -- this handler is a SIBLING to that
    one, gated on the opposite predicate (continuation, not fork), and
    asserts no supersession of it. The C0 claude-klabauter-em seam decision
    (dispatch/supersede/coexist between baton_assemble and author_fork) is
    still open and out of scope for this directive.

    DR-242 gate (`docs/decisions/DR-242-successor-named-child-is-not-evidence-
    of-succ.md`, § C5a of `docs/plans/2026-07-28-handoff-close-path-fail-
    loud.md`): before composing the op at all, checks
    `coordinator_core.archival.claimed_or_shipped_at_path` on
    `predecessor_path` -- this handler names its own successor as the
    predecessor's `continued_into`, which is exactly the successor-named-child
    evidence DR-242 forbids treating as sufficient on its own.

    THAT GATE DEGRADES; IT DOES NOT RAISE (2026-08-03 break-class fix, live
    repro below). It is a NOT-APPLICABLE, not a failure, and the distinction is
    the same one `_dispatch_render_project_tracker` above already draws: the op
    is never composed, so nothing is half-applied and the predecessor is left
    byte-identical. A predecessor that was never claimed or shipped is, per
    DR-242 itself, not in a succession relationship at all -- there is nothing
    to strand. The refusal's substance is unchanged (no unclaimed predecessor is
    ever superseded, by this path or any other); only its blast radius is.

    What makes softening THIS check safe is that it was never the load-bearing
    one: AC8 moved the gate down to the op's own choke point
    (`ops/handoff_archive_transition.py`'s `if mode == "supersede":` block,
    whose own comment names this handler as one of three wrapper-level checks
    "none of them a load-bearing choke point", kept as defense in depth). The
    check here is an early return that spares composing an op certain to
    refuse -- so it answers the same question the choke point does, and the
    only open question was what to do with the answer.

    Live repro (claude-klabauter, 2026-08-03, `/handoff` over a `status: open`
    predecessor): the gate raised, `_cleanup_successor` deleted the successor
    d1 had just minted, `_D1_COMPENSATORS` then ran, and the operator's report
    said `status: partial` with `compensation: [d5, d1] succeeded: true` -- a
    rollback that reported success for destroying the one irreversible artifact
    of the ceremony. Worse, the gate is DETERMINISTIC on the same inputs, so the
    sanctioned resume path (re-run the identical command) could never converge:
    every attempt minted and then deleted the same file. Under context pressure
    that is the exact save-state loss `/handoff` exists to prevent, traded for a
    succession edge that was never going to be written either way.

    The two paths below stay RAISES, and must: both are reached only after the
    op has actually been composed, where "the predecessor may be half-stamped"
    is live and `superseded is False` is the stranding defect this directive
    exists to eliminate.
    """
    predecessor_path, continued_into, exclude_path = (list(args) + ["", "", ""])[:3]

    def _cleanup_successor() -> None:
        """Removes the successor d1 scaffolded earlier in THIS run, before this
        handler raises -- but only when the file is still a pristine generator
        scaffold.

        The pristine gate is the SAME predicate `_compensate_d1_scaffold`
        consults (`_is_pristine_generator_scaffold`), composed rather than
        restated, and it closes a break-class hole this cleanup carried
        unconditionally: `--out` is not always a file this run created. On a
        replay it is the successor a PRIOR attempt scaffolded
        (`_resume_recorded_successor_path`), and since 2026-07-30 it may be an
        adopted prior attempt carrying real operator prose
        (`_adopt_prior_attempt_scaffold_path`, DR-242 Amendment A1) -- d1 is
        `already_satisfied` in both cases and never wrote the file at all.
        Unlinking it on any d6 failure (an unrelated live child, a lock
        timeout, an unexpected OSError) destroyed operator content this run had
        no claim to. A stranded PRISTINE scaffold is still cleaned up, which is
        what this cleanup was written for.
        """
        # Review: code-reviewer -- the literal "handoff" here is intentional,
        # not a divergence from `_compensate_d1_scaffold`'s `--type`-routed
        # doc_type lookup: this handler (d6) is reachable ONLY for
        # kind="handoff" today (`_build_directives` never emits d6 for
        # kind=spinoff -- see resolve_lineage's own "kind=spinoff has no
        # predecessor and therefore never resumes" comment), so there is no
        # other doc_type this call could ever see. If d6 is ever generalized
        # to a non-handoff kind (the open C0 claude-klabauter-em seam decision noted
        # above), this hardcode must be routed through the same `--type`-flag
        # lookup `_compensate_d1_scaffold` already uses, or it will silently
        # diverge between the two compensator call sites for what should be
        # the same predicate.
        if exclude_path and _is_pristine_generator_scaffold(
            repo_root / exclude_path, "handoff"
        ):
            successor_abs = repo_root / exclude_path
            try:
                successor_abs.unlink()
            except FileNotFoundError:
                pass

    from coordinator_core.archival import claimed_or_shipped_at_path

    if not claimed_or_shipped_at_path(str(repo_root / predecessor_path)):
        print(
            "baton-assemble apply: handoff.supersede_predecessor degraded -- "
            f"{predecessor_path!r} was never claimed or shipped, so DR-242 leaves "
            "it nothing to supersede (a successor-named child is not evidence of "
            "succession). The predecessor was left exactly as it was; the mint and "
            "every other directive in this run proceeded normally. If that "
            "predecessor SHOULD have been superseded, claim it and re-run -- do "
            "not hand-stamp the succession.",
            file=sys.stderr,
        )
        return {
            "cli": "handoff.supersede_predecessor",
            "args": args,
            "result": None,
            "degraded": {
                "reason": "predecessor-not-claimed-or-shipped",
                "predecessor": predecessor_path,
            },
        }

    # 2026-07-27 review fix (Finding 2): the successor-cleanup below must
    # fire on ANY failure to compose this op -- including an exception
    # raised out of _invoke_op_in_process itself (e.g. an unexpected OSError,
    # a lock timeout not already converted to a dict return), not only the
    # deliberate `superseded is False` return path. Without this try/except,
    # an exception here leaves the already-scaffolded successor stranded on
    # disk, contradicting this handler's own docstring claim ("Also removes
    # the successor artifact ... before raising").
    try:
        result = _invoke_op_in_process(
            "handoff.archive_transition",
            {
                "handoff_path": predecessor_path,
                "mode": "supersede",
                "continued_into": continued_into,
                # ABSOLUTE, resolved against this call's own `repo_root` --
                # never the repo-relative token `_build_directives` emitted.
                # `dag.referenced_by` filters its live set with
                # `os.path.abspath(str(ex))`, which resolves a relative
                # `exclude` against the PROCESS CWD, not the worktree root
                # (`coordinator_core/dag.py:~1513`). Any `apply` invoked from a
                # subdirectory of the repo therefore excluded a path that does
                # not exist, the live-children guard saw this run's OWN
                # successor as an unrelated live child (it names the
                # predecessor via `predecessor:`, which d1 stamps), and
                # `handoff.archive_transition` retained the predecessor instead
                # of archiving it -- a silent half-succession, reported
                # `superseded: True, moved: False`. `continued_into` stays
                # repo-relative: it is FRONTMATTER, contractually
                # repo-relative, and an absolute value there would author a
                # machine-specific edge.
                "exclude": [str(repo_root / exclude_path)] if exclude_path else [],
            },
            repo_root,
        )
    except Exception:
        _cleanup_successor()
        raise

    if not result.get("superseded"):
        _cleanup_successor()
        raise RuntimeError(
            "handoff.archive_transition mode='supersede' did not supersede "
            f"{predecessor_path!r} (superseded=False; exit_code="
            f"{result.get('exit_code')!r}, retained={result.get('retained')!r}, "
            f"retain_reason={result.get('retain_reason')!r}, "
            f"error={result.get('error')!r}) -- aborting this mint rather than "
            "leaving a half-applied succession"
        )
    return {"cli": "handoff.supersede_predecessor", "args": args, "result": result}


#: THE closed dispatch table -- every key is a literal string written here
#: by hand; this dict is never mutated at runtime and never consulted via
#: anything but a plain `dict.get`/`in` on a `directives[].cli` value.
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "coordinator-doc-new": _dispatch_coordinator_doc_new,
    "lint-frontmatter": _dispatch_lint_frontmatter,
    "session-claim-cli": _dispatch_session_claim_cli,
    "handoff.stamp_phase": _dispatch_handoff_stamp_phase,
    "handoff.author_fork": _dispatch_handoff_author_fork,
    "render-project-tracker": _dispatch_render_project_tracker,
    "handoff.supersede_predecessor": _dispatch_handoff_supersede_predecessor,
}


def _resolve_cli(cli_name: str) -> Callable[[list[str], Path], dict[str, Any]]:
    return apply_base.resolve_cli(_CLI_DISPATCH, cli_name)


#: doc_type (d1's own `--type=<kind>` token, i.e. `baton_assemble.KINDS`) ->
#: the name of the scaffolder function inside `coordinator-doc-new` that
#: renders it. A closed literal table, written by hand: the value is looked up
#: with `getattr` on the loaded generator module, so a caller-derived string
#: must never reach it (same discipline `_CLI_DISPATCH` above holds for `cli`
#: names). Only the two kinds `_build_directives` can emit d1 for are listed;
#: an unlisted doc_type makes `_is_pristine_generator_scaffold` decline.
_GENERATOR_SCAFFOLDERS: dict[str, str] = {
    "handoff": "_scaffold_handoff",
    "spinoff": "_scaffold_spinoff",
}

#: The generator's scaffolder parameters, paired with the frontmatter field each
#: one round-trips through. Read in this direction ONLY -- from a rendered
#: scaffold back to the arguments that produced it -- so that
#: `_is_pristine_generator_scaffold` can re-render without re-deriving what a
#: template looks like. Every entry is a field the scaffolder itself emits
#: verbatim from its own parameter; `created` is deliberately absent (the
#: scaffolder reads `_today()` internally rather than taking it as a parameter,
#: which is what makes a scaffold minted before a UTC midnight roll decline).
_SCAFFOLD_PARAM_FIELDS: tuple[tuple[str, str], ...] = (
    ("title", "title"),
    ("branch", "branch"),
    ("deliverable_id", "deliverable_id"),
    ("initiative", "initiative"),
    ("handoff_id", "handoff_id"),
    ("origin_handoff_id", "origin_handoff_id"),
    ("predecessor", "predecessor"),
    ("predecessor_id", "predecessor_id"),
    ("category", "category"),
)

#: `deliverable_id`/`initiative` are D9 present-as-null in the generator's own
#: emission (`_yaml_quote(x) if x else "null"`), so the literal `null` read back
#: off a pristine scaffold means "this parameter was None", not "this parameter
#: was the string 'null'". Inverting that is the one place this module has to
#: know an emission convention, and it is the generator's own documented one.
_SCAFFOLD_NULL_SENTINELS = frozenset({"null", "~", ""})

_DOC_NEW_MODULE: dict[str, Any] = {}


def _resolve_claude_klabauter_bin() -> Path:
    """`claude_klabauter_bin` for the generator-load seam below -- the SAME
    `resolve_operator_config()` key `_dispatch_coordinator_doc_new` spawns
    `coordinator-doc-new` out of, so the module the pristine-scaffold predicate
    re-renders through and the CLI d1 actually ran are the same file by
    construction, not by coincidence.

    Extracted as its own function purely so the resolution is one named seam a
    test can pin (this suite quarantines HOME, which makes the real
    `resolve_operator_config()` fail loud by design) -- it is NOT a fallback
    ladder, and it must never grow one: an unresolvable settings-home raises,
    and every caller of `_is_pristine_generator_scaffold` treats that as
    "generator unavailable, decline to delete".
    """
    from coordinator_core.resolution.facade import resolve_operator_config

    return Path(resolve_operator_config()["claude_klabauter_bin"])


def _load_doc_new_module() -> Any:
    """Loads `coordinator-doc-new` as a module (cached for this process) so its
    scaffolder functions can be CALLED rather than re-implemented.

    Resolved through `resolve_operator_config()["claude_klabauter_bin"]` -- the SAME
    resolution `_dispatch_coordinator_doc_new` above uses to spawn it, so the
    module this compensator reasons about and the CLI d1 actually ran can never
    be two different files. `SourceFileLoader` is passed explicitly because the
    script is extensionless and `spec_from_file_location` cannot infer a source
    loader from a suffixless path (the identical fix
    `workstream_complete.apply._load_cli_module` carries; the module is
    registered in `sys.modules` before `exec_module` for the same
    dataclass-forward-ref reason documented there).

    Negative-spec: does NOT spawn a subprocess, does NOT call the generator's
    `main()`, and does NOT write anything -- only the pure scaffolder functions
    named in `_GENERATOR_SCAFFOLDERS` are ever invoked off the loaded module.
    """
    cached = _DOC_NEW_MODULE.get("module")
    if cached is not None:
        return cached

    import importlib.machinery
    import importlib.util

    script_path = _resolve_claude_klabauter_bin() / "coordinator-doc-new"
    module_name = "_baton_assemble_coordinator_doc_new"
    loader = importlib.machinery.SourceFileLoader(module_name, str(script_path))
    spec = importlib.util.spec_from_file_location(module_name, script_path, loader=loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"baton_assemble.apply: could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _DOC_NEW_MODULE["module"] = module
    return module


def _render_pristine_scaffold(doc_type: str, fm_text: str) -> Optional[str]:
    """Re-renders what `coordinator-doc-new --type=<doc_type>` WOULD have
    written for a file whose frontmatter is `fm_text`, by calling the
    generator's own scaffolder with the parameter values read back off that
    frontmatter. Returns `None` when the render is not obtainable.

    Why this composes rather than duplicates. The generator's scaffold output is
    a pure function of its scaffolder parameters plus `_today()` -- no hidden
    state, no I/O (see `coordinator/bin/coordinator-doc-new`'s
    `_scaffold_handoff`/`_scaffold_spinoff`). Every one of those parameters is
    emitted verbatim into a field of the very frontmatter it produces, so a
    PRISTINE scaffold carries, on its own face, the complete argument vector
    that generated it. Reading that vector back and re-invoking the SAME
    function is therefore a total, exact reproduction -- not a heuristic, and
    not a second definition of "what a template looks like". The template's one
    definition stays in the generator; this function only inverts the argument
    binding.

    The trailing-newline top-up mirrors the generator's own write site
    (`fh.write(content)` then one `"\\n"` if absent) rather than assuming the
    scaffolder's return value is already newline-terminated.

    Negative-spec:
      - Does NOT record, cache, or persist the rendered text anywhere. The
        resume model forbids a second source of truth (see this module's
        docstring, RESUME MODEL); the render is recomputed from the file and the
        generator on every call and thrown away.
      - Does NOT infer any parameter the frontmatter does not carry, and does
        NOT normalize/repair a value on the way back in -- an absent field maps
        to `None` (the scaffolder's own omit-when-absent convention), never to a
        guess.
      - Does NOT compare anything. Callers do the comparison; this function's
        one job is the render.
    """
    scaffolder_name = _GENERATOR_SCAFFOLDERS.get(doc_type)
    if scaffolder_name is None:
        return None
    module = _load_doc_new_module()
    scaffolder = getattr(module, scaffolder_name, None)
    if scaffolder is None:
        return None

    import inspect

    accepted = inspect.signature(scaffolder).parameters
    kwargs: dict[str, Any] = {}
    for param, field in _SCAFFOLD_PARAM_FIELDS:
        if param not in accepted:
            # e.g. `_scaffold_spinoff` takes no `predecessor` -- it emits
            # `predecessor: none` unconditionally, so the field is not an
            # inverted parameter there.
            continue
        value = read_fm_field_unquoted(fm_text, field)
        if value is not None:
            value = value.strip()
        if value is None or value in _SCAFFOLD_NULL_SENTINELS:
            value = None
        kwargs[param] = value
    if kwargs.get("title") is None or kwargs.get("branch") is None:
        # Both are required positional parameters with no default and no
        # omit-when-absent convention; a file missing either is not a scaffold
        # this function can reproduce.
        return None

    rendered = scaffolder(**kwargs)
    if not isinstance(rendered, str):
        return None
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _is_pristine_generator_scaffold(target: Path, doc_type: str) -> bool:
    """Does `target` carry NOTHING beyond what the scaffold generator itself
    produced? True only when the file is byte-identical to a re-render of
    `coordinator-doc-new`'s own template for the same doc-type and the same
    argument vector the file itself records.

    This replaces the predicate this module shipped with on 2026-07-29, which
    asked a DIFFERENT question: whether the file still carried both of
    `coordinator-doc-new`'s no-`--title` template defaults (`title` prefixed
    `"PLACEHOLDER"`, `handoff_id` containing `"placeholder"`). That used "a
    title was supplied" as a proxy for "the operator has content worth
    protecting", and the two are not the same fact -- supplying `--title` alone
    flipped a scaffold to operator-customized and made it survive even when the
    operator had written nothing at all. A titled-but-otherwise-untouched
    scaffold has nothing to protect.

    FAIL-SAFE, in one direction only: every uncertainty answers False (do not
    delete). Unreadable file, absent/malformed frontmatter, a doc_type outside
    `_GENERATOR_SCAFFOLDERS`, an unresolvable/unloadable generator, a scaffolder
    that refuses its own arguments (`_scaffold_handoff` calls `sys.exit(1)` for
    `predecessor_id` without `predecessor`, and `_validate_category` exits on an
    off-enum category -- hence the `SystemExit` catch, which must NOT be allowed
    to tear down the caller), or a `created:` date that has since rolled past
    UTC midnight all decline. Destroying operator content is the unrecoverable
    direction; a surviving orphan file is not -- and since 2026-07-29 an orphan
    is additionally recoverable, because the re-run ADOPTS it (see
    `_adopt_prior_attempt_scaffold_path` and DR-242 Amendment A1).

    2026-07-30 observability fix (review: code-reviewer, Finding 2): the
    "generator unavailable" decline (`_load_doc_new_module` raising
    `FileNotFoundError`/`OSError` off a missing script, `ImportError` off a
    malformed spec, or `resolve_operator_config` raising `OperatorConfigError`
    off a corrupt settings-home) stays a SILENT decline, same as `SystemExit`
    -- these are the two expected-refusal shapes named above. Anything else
    (an `AttributeError`/`TypeError` from a signature/field drift between this
    module's own `_SCAFFOLD_PARAM_FIELDS` and the generator's real scaffolder
    parameters, for instance) is a defect in the predicate itself, not a
    legitimate decline, and was previously indistinguishable from the expected
    cases -- a broken predicate could silently self-disable forever, orphans
    accumulating with nothing in logs pointing at why. That class is now
    printed to stderr (this module's existing stderr-for-non-fatal-advisory
    convention, see `_dispatch_handoff_author_fork`'s and
    `_dispatch_render_project_tracker`'s degrade prints) naming the file and
    the exception, and STILL declines -- the fail-safe direction is unchanged,
    only its silence for the unexpected case is removed.

    Negative-spec: does NOT read, write, or consult any sidecar, hash file, or
    run-state -- the entire input is `target`'s own bytes plus the generator.
    """
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    split = split_frontmatter(text)
    if split is None:
        return False

    from coordinator_core.resolution.facade import OperatorConfigError

    try:
        rendered = _render_pristine_scaffold(doc_type, split.fm_text)
    except SystemExit:
        return False
    except (ImportError, OSError, OperatorConfigError):
        # Generator unavailable -- `_load_doc_new_module` raising on a missing
        # script or malformed spec, or `resolve_operator_config` raising on a
        # corrupt settings-home. Expected decline, stays silent (see docstring
        # 2026-07-30 note above).
        return False
    except Exception as exc:
        print(
            f"baton-assemble apply: _is_pristine_generator_scaffold declined "
            f"on {target} -- predicate raised {exc!r}, which is NOT one of "
            "the expected refusal shapes (scaffolder SystemExit, generator "
            "unavailable) -- treat this as a likely bug in the predicate "
            "itself, not a legitimate decline. Declining to delete regardless.",
            file=sys.stderr,
        )
        return False
    return rendered is not None and rendered == text


def _d1_doc_type(directive: dict[str, Any]) -> Optional[str]:
    """The `--type=<kind>` token off a d1 directive's own args -- the doc_type
    d1 asked the generator for, never a re-derivation from the file's `kind:`
    field."""
    return next(
        (
            arg[len("--type="):]
            for arg in directive.get("args", []) or []
            if isinstance(arg, str) and arg.startswith("--type=")
        ),
        None,
    )


def _compensate_d1_scaffold(
    directive: dict[str, Any], repo_root: Path, detail: Optional[dict[str, Any]]
) -> None:
    """Compensator for kind=handoff/spinoff's d1 (`coordinator-doc-new`),
    registered into `apply_base.execute_directives`'s optional
    `compensators` seam via `_D1_COMPENSATORS` below. Runs ONLY when d1
    landed THIS run and a LATER directive then failed
    (`APPLY_EXIT_PARTIAL_MUTATION`) -- deletes the scaffold d1 minted this
    run, closing the gap `_degrade_placeholder_scaffold_after_partial_
    failure` (2026-07-29, retired by this same fix) only partly closed: that
    function flipped a stranded scaffold's `pickup_ready` to `false` but
    left the empty file itself on disk forever, one per failed run (a fresh
    `_compute_fresh_output_path` HHMMSS-disambiguated path each time, since a
    re-run never reuses a prior failed run's path). This compensator instead
    removes the file outright when it can PROVE the file is this run's own,
    never-populated scaffold.

    Guards (all required):
      - d1's own `args` must carry a `--out=<path>` token (the fresh path
        `_build_directives` computed for THIS run's scaffold) -- absent it,
        there is nothing to resolve and this is a no-op.
      - d1's own `args` must carry a `--type=<kind>` token, so the re-render
        below asks the generator for the SAME doc-type d1 did.
      - The file must be a PRISTINE generator scaffold --
        `_is_pristine_generator_scaffold`, i.e. byte-identical to a re-render
        of `coordinator-doc-new`'s own template for the argument vector the
        file itself records. A file carrying anything the generator did not
        write (edited body, replaced summary, added field) is real operator
        work and is left on disk untouched.

        Replaces (2026-07-30) the two-marker placeholder check this
        compensator shipped with: `title` prefixed `"PLACEHOLDER"` AND
        `handoff_id` containing `"placeholder"`, which are the generator's
        no-`--title` defaults. That predicate treated "a title was supplied"
        as a proxy for "there is content worth protecting" -- so
        `--title` ALONE made an otherwise-untouched scaffold survive, which
        was most of the pre-d6 orphan edge
        (`_adopt_prior_attempt_scaffold_path` handles what remains).
      - Deletion is a plain `Path.unlink()` -- never `git rm`, never an
        index touch: this run's `_scoped_commit` never ran (partial-
        mutation exits skip it), so the file was never staged or committed
        in the first place.

    Best-effort: any failure reading/parsing the file (missing, malformed
    frontmatter, I/O error) is a silent no-op, matching the retired flip's
    own posture -- this runs as a REACTION to an already-failed run, and
    `apply_base.execute_directives`'s own compensator try/except additionally
    catches and records anything this function itself raises, so it never
    masks the original partial-mutation error either way.
    """
    out_path = next(
        (
            arg[len("--out="):]
            for arg in directive.get("args", []) or []
            if isinstance(arg, str) and arg.startswith("--out=")
        ),
        None,
    )
    if not out_path:
        return
    doc_type = _d1_doc_type(directive)
    if not doc_type:
        return
    target = repo_root / out_path
    if not target.is_file():
        return
    if not _is_pristine_generator_scaffold(target, doc_type):
        return
    target.unlink()


def _compensate_d5_release_claim(
    directive: dict[str, Any], repo_root: Path, detail: Optional[dict[str, Any]]
) -> None:
    """Compensator for kind=handoff's d5 (`session-claim-cli release-artifact
    plan <slug>`), registered into `apply_base.execute_directives`'s optional
    `compensators` seam via `_D1_COMPENSATORS` below (C5, 2026-08-02 --
    docs/plans/2026-08-02-roadmap-baton-supersession-hazard.md, § F5). Runs
    ONLY when d5 landed THIS run (genuinely released the session's plan
    claim into the unversioned `<git-common-dir>/coordinator-sessions/`
    store, which `git revert` cannot restore) and a LATER directive then
    failed (`APPLY_EXIT_PARTIAL_MUTATION`) -- re-acquires the same plan claim
    so an aborted d6 does not leave the session's own execution lock
    stranded on top of a half-applied succession.

    EM RULING (option (a) of the plan's two offered shapes): a d5
    compensator here, rather than hoisting d6's gate evaluation ahead of
    d5's execution in `baton_assemble/__init__.py` (option (b)) -- that file
    is owned by peer chunks C3/C4 this same run, so option (a) keeps this
    fix disjoint from their edits. Satisfies AC6 ("d5's claim release is
    compensated on abort") on its own terms.

    SCOPE LIMIT (verified against `coordinator/bin/session-claim-cli` and
    `coordinator_core/session/claims.py`): `session-claim-cli` has NO
    reacquire/reclaim/restore verb. The only re-acquisition path is
    re-running `claim-artifact plan <stem>` / `claims.claim_plan`, and
    neither takes a session-id parameter -- they stamp the CURRENTLY
    RUNNING session's id via `core.resolve_session_id(cwd)`. Consequence:
    this compensator correctly restores ownership when running in the SAME
    session that released the claim (the normal abort case -- `apply()`
    always runs in-process in the session that dispatched it, and `plan` is
    the one claim class `claim_artifact` documents a same-session
    re-entrant branch for). It does NOT restore the ORIGINAL session's
    ownership on a cross-session replay. Do not read this as a general
    restore. After a completed `release-artifact` the claim dir is gone, so
    the re-claim's `os.mkdir` succeeds cleanly; liveness only enters on
    EEXIST, where `claim_artifact` evaluates the HOLDER's liveness, not the
    caller's.

    Guards (all required, mirroring `_compensate_d1_scaffold`'s own
    best-effort posture):
      - `directive["args"]` must be the 3-slot shape `_build_directives`
        emits for d5 (`["release-artifact", "plan", <slug>]`) -- anything
        else is not a directive this compensator can reason about, and it
        no-ops rather than guessing.
      - The slug is passed through `claims.claim_plan` unchanged; that
        function's own shape validation (bare basename, no path separator,
        no `.md` suffix) is the one authority on what a legal slug looks
        like, not re-derived here.

    Best-effort, matching `_compensate_d1_scaffold`'s own posture: a raise
    out of `claim_plan` (or an import failure) propagates to
    `apply_base.execute_directives`'s own compensator try/except, which
    records it under `report["compensation"]` and never lets it mask the
    original partial-mutation error.
    """
    args = directive.get("args") or []
    if len(args) != 3 or args[0] != "release-artifact" or args[1] != "plan":
        return
    slug = args[2]
    if not slug:
        return

    from coordinator_core.session import claims as session_claims

    session_claims.claim_plan(slug, cwd=str(repo_root))


#: Registered into `apply_base.execute_directives`'s optional `compensators`
#: seam -- keyed by THIS module's own directive id (`"d1"`/`"d5"`), never a
#: `cli` name, since a `cli` (e.g. `coordinator-doc-new`) is shared across
#: directives whose compensation need differs by role in the envelope, not
#: by which CLI they happen to invoke.
_D1_COMPENSATORS: dict[str, Callable[[dict[str, Any], Path, Optional[dict[str, Any]]], None]] = {
    "d1": _compensate_d1_scaffold,
    "d5": _compensate_d5_release_claim,
}


def _execute_directives(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    repo_root: Path,
    *,
    decisions: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """Thin wrapper binding `apply_base.execute_directives`'s generic
    `dispatch_table` parameter to this module's own closed `_CLI_DISPATCH`,
    and its optional `compensators` parameter to `_D1_COMPENSATORS`."""
    return apply_base.execute_directives(
        directives,
        judgment_points,
        repo_root,
        _CLI_DISPATCH,
        decisions=decisions,
        compensators=_D1_COMPENSATORS,
    )


def _resolve_explicit_session_id(session_id: Optional[str]) -> Optional[str]:
    return apply_base.resolve_explicit_session_id(session_id, env_read_order=_SESSION_ENV_READ_ORDER)


def _session_identity(session_id: str):
    return apply_base.session_identity(session_id, env_vars=_SESSION_ENV_VARS)


#: exit_code -> caller-legible `status` label, stamped onto every `apply()`
#: return by `_finalize_report` below. Closes a distinct defect from the
#: `--help`/flag-shape one above: a partially-applied run (some directives
#: landed, one raised) used to surface as one bare `{"error": ..., "failed_
#: directive": ...}` string -- distinguishable from a clean run only by
#: knowing this module's private exit-code enum. `status` makes "fully
#: applied" vs "partially applied" vs "nothing applied" legible in the JSON
#: body itself, not only in the process exit code a caller may not check.
#:
#: A DEGRADE is deliberately not a status of its own: a run whose only
#: non-success is a directive declining for a known reason DID fully apply,
#: and reports `"ok"`. "Did anything decline" is answered by the report's
#: top-level `degraded` list instead (see `_collect_degraded`) -- a separate
#: question from "did every directive that ran apply", which is what these
#: labels answer. Do NOT add a `"degraded"` label here: it would make a
#: clean-but-declined run indistinguishable from a broken one for every
#: caller that branches on `status`, which is the confusion this pair of
#: surfaces exists to remove.
_STATUS_LABELS: dict[int, str] = {
    APPLY_EXIT_OK: "ok",
    apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT: "halted_at_judgment",
    apply_base.APPLY_EXIT_CLAIM_DENIED: "claim_denied",
    APPLY_EXIT_TRANSPORT_FAIL: "transport_fail",
    apply_base.APPLY_EXIT_PARTIAL_MUTATION: "partial",
}


def _finalize_report(exit_code: int, report: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Single chokepoint every `apply()` return path funnels through.
    Guarantees `landed` is always present as a list -- including on an
    early transport-failure return that never reached directive dispatch,
    where nothing landed but the key was previously just absent -- and
    stamps `status` (see `_STATUS_LABELS`) so a partially-applied run
    (`d1`/`d2` landed, `d4` failed) is never mistaken for a clean one by a
    caller that only reads the `error` field."""
    report = dict(report)
    report.setdefault("landed", [])
    report["status"] = _STATUS_LABELS.get(exit_code, "unknown")
    return exit_code, report


def _collect_directive_commits(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Names every commit this run produced that `apply()` did NOT make
    itself, so the report accounts for the whole git history the run wrote.

    2026-07-29 legibility fix. `report["commit_sha"]` names ONE commit -- the
    `_scoped_commit` over the successor artifact -- but d6 (`handoff.
    supersede_predecessor`, composing `handoff.archive_transition`) does its
    own `git mv` + commit internally. An operator reading a report with a
    single `commit_sha` after a run that produced two commits has no way to
    tell whether the predecessor's rename is committed or sitting unstaged
    with its deletion side stranded -- so they go and find out by hand
    (observed live, 2026-07-29: a `git log` + two-path history walk that the
    report should simply have answered).

    `handoff.archive_transition` returns `moved: bool` and no sha (see that
    module's return contract), so this reports the fact and the committing
    directive, never a fabricated sha. An archived-predecessor stamp-in-place
    performs no move and therefore contributes no entry.
    """
    commits: list[dict[str, Any]] = []
    for result in report.get("results", []) or []:
        detail = result.get("detail") or {}
        if detail.get("cli") != "handoff.supersede_predecessor":
            continue
        if (detail.get("result") or {}).get("moved"):
            commits.append(
                {
                    # `"id"`, not `"directive_id"` -- `apply_base.
                    # DirectiveResult.to_report()` is the one authority on this
                    # report row's key names. This read said `"directive_id"`
                    # when it landed (21aae033), so the entry it built always
                    # reported `None` for the very directive it exists to name;
                    # its own test hand-built the row with the same wrong key
                    # and so agreed with the bug. Any future reader of a
                    # `results[]` row takes its key names from `to_report`.
                    "directive_id": result.get("id"),
                    "committed_by": "handoff.archive_transition",
                    "what": "predecessor git-mv into archive/handoffs/, committed by the op itself",
                    "sha": None,
                }
            )
    return commits


def _collect_degraded(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Hoists every directive-level degrade to ONE top-level report key, the
    same legibility contract `_collect_directive_commits`/`_collect_replayed_
    directives` serve for commits and replays.

    2026-08-03. Three handlers here degrade rather than raise (d3's
    `handoff.author_fork` stamp-mode field degrades, d4's
    `render-project-tracker`, and d6's DR-242 gate as of this same fix), and a
    degrade correctly leaves `status: "ok"` -- the run applied. The cost was
    that the ONLY machine-readable trace sat at
    `report["results"][i]["detail"]["degraded"]`, two levels down a list a
    caller has to iterate itself. Read live: an operator scanning a report for
    "did anything not happen" found either a bare `ok` or (when an unrelated
    directive had failed) a `partial` they then misattributed to the degrade.
    `status` answers "did every directive that ran apply"; this key answers
    "did any directive decline", and the two are genuinely different questions.

    Additive only -- no existing key changes meaning or shape, and a run with
    no degrades reports `[]` rather than omitting the key, so "nothing
    declined" and "this report does not say" stay distinguishable. Normalizes
    both in-tree degrade shapes (a single dict; d3's list-of-field-degrades)
    into one flat list of rows.
    """
    degraded: list[dict[str, Any]] = []
    for result in report.get("results", []) or []:
        detail = result.get("detail") or {}
        entry = detail.get("degraded")
        if not entry:
            continue
        rows = entry if isinstance(entry, list) else [entry]
        for row in rows:
            degraded.append(
                {
                    # `"id"` -- `apply_base.DirectiveResult.to_report()` is the
                    # one authority on this row's key names.
                    "directive_id": result.get("id"),
                    "cli": detail.get("cli"),
                    **(row if isinstance(row, dict) else {"reason": str(row)}),
                }
            )
    return degraded


def _collect_replayed_directives(
    directives: list[dict[str, Any]], report: dict[str, Any]
) -> list[dict[str, Any]]:
    """Names every directive this run reported as `landed` WITHOUT dispatching
    its handler, and why -- the replay half of the same legibility contract
    `_collect_directive_commits` above serves for commits.

    2026-07-29 (idempotent-replay fix). `apply_base.execute_directives`'s
    contract is that an `already_satisfied` directive still appears in
    `results`/`landed` -- deliberately, so a replay's report is comparable to a
    clean run's. The cost is that a converged re-run and a clean run are
    indistinguishable from `landed` alone: both say "d1, d2, d4, d5, d6". An
    operator re-running `apply` after a partial abort is specifically asking
    "what did this actually do the second time" and must not be answered with a
    silent green.

    The reason string is NOT derived here -- it is
    `directives[].already_satisfied_reason`, written by `_build_directives` at
    the one place the satisfaction predicate is evaluated. This function only
    joins it to the execution result, so there is no second definition of why a
    directive was skipped.
    """
    reasons = {
        d.get("id"): d.get("already_satisfied_reason")
        for d in directives
        if d.get("already_satisfied")
    }
    replayed: list[dict[str, Any]] = []
    for result in report.get("results", []) or []:
        if not result.get("already_satisfied"):
            continue
        # `to_report()` keys this "id" (see apply_base.DirectiveResult).
        directive_id = result.get("id")
        replayed.append(
            {
                "directive_id": directive_id,
                "reason": reasons.get(directive_id),
            }
        )
    return replayed


def _report_replay_to_stderr(replayed: list[dict[str, Any]]) -> None:
    """Prints the replay account to stderr, matching this module's existing
    stderr-for-non-fatal-advisory convention (`_usage`, the author_fork degrade
    print, the render-project-tracker degrade print). stdout stays the machine
    surface -- `main_apply` prints the JSON report there and nothing else."""
    if not replayed:
        return
    ids = ", ".join(str(entry["directive_id"]) for entry in replayed)
    print(
        f"baton-assemble apply: REPLAY -- {len(replayed)} directive(s) already "
        f"satisfied on disk and reported as landed WITHOUT re-dispatching: {ids}. "
        "This run is continuing a prior partially-applied run rather than "
        "starting one.",
        file=sys.stderr,
    )
    for entry in replayed:
        print(
            f"  {entry['directive_id']}: {entry['reason'] or 'no reason recorded'}",
            file=sys.stderr,
        )


def _compute_commit_message(kind: str, basename: str, landed: list[str]) -> str:
    summary = ", ".join(landed) if landed else "no-op"
    return f"baton-assemble apply: {kind} {basename} ({summary})"


def _scoped_commit(repo_root: Path, artifact_rel_path: str, kind: str, basename: str, landed: list[str]) -> Optional[str]:
    message = _compute_commit_message(kind, basename, landed)
    return apply_base.scoped_commit(repo_root, artifact_rel_path, message, _run_git)


def apply(
    kind: str,
    artifact_path: str,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    decisions: Optional[dict[str, Any]] = None,
    title: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """`apply <kind> <artifact-path> [--session-id <id>] [--decisions <json>]
    [--title <text>]` -- recomputes the brief in-process and executes its
    `directives[]` through the closed dispatch table via
    `apply_base.execute_directives`. Returns `(exit_code, report)`;
    `report["landed"]` names exactly which directive ids mutated state.
    `title`, when supplied, is forwarded to `brief()` so d1's scaffold
    directive carries `--title=<title>`; omitted entirely otherwise."""
    from coordinator_core.baton_assemble import TransportFailure, brief, resolve_repo_root

    root = repo_root or resolve_repo_root()
    if root is None:
        return _finalize_report(
            APPLY_EXIT_TRANSPORT_FAIL, {"error": "could not resolve a git worktree root"}
        )

    resolved_sid = _resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return _finalize_report(
            APPLY_EXIT_TRANSPORT_FAIL,
            {
                "error": (
                    "no session id resolvable via --session-id or "
                    f"{'/'.join(_SESSION_ENV_READ_ORDER)} -- refusing the ambient tier-4 sentinel"
                ),
            },
        )

    with _session_identity(resolved_sid):
        try:
            brief_result = brief(kind, artifact_path, decisions=decisions, repo_root=root, title=title)
        except TransportFailure as exc:
            return _finalize_report(APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)})
        except ValueError as exc:
            return _finalize_report(APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)})

        decision = brief_result.decision_object
        directives = decision.get("directives", [])
        judgment_points = decision.get("judgment_points", [])
        # `brief()` normalizes a bare-slug `artifact_path` (see
        # `baton_assemble._normalize_artifact_path`) and threads the resolved
        # value back into the envelope's own `artifact.path` -- the RAW
        # `artifact_path` parameter above is legitimately used ONLY to
        # recompute the brief; anything read back out must use the normalized
        # value the envelope reports, never the raw parameter. (Historically
        # this fed the `_scoped_commit` pathspec too, because a bare-slug
        # invocation otherwise staged a nonexistent path while d1 scaffolded
        # the normalized one. The pathspec now comes from
        # `lineage["output_path"]` instead -- a superset of that fix, since the
        # two are equal in exactly the bare-slug case it was written for; see
        # the block below. This value survives as the input-side identity and
        # as that block's malformed-envelope fallback.)
        artifact_meta = decision.get("artifact")
        effective_artifact_path = (
            artifact_meta.get("path") if isinstance(artifact_meta, dict) else None
        ) or artifact_path

        # THE COMMIT PATHSPEC IS THE PATH THIS RUN WROTE, not the path it READ.
        #
        # 2026-07-29 break-class fix, found by the idempotent-replay suite. The
        # pathspec used to be `effective_artifact_path` -- the INPUT artifact
        # (`artifact.path`, the predecessor handoff or the plan being handed
        # off). In the bare-slug mint convention `output_path` and
        # `artifact_path` coincide, which is why that read as correct for as
        # long as it did; in every OTHER shape it staged the wrong file. Two
        # separate consequences, both live:
        #
        #   1. The freshly-minted successor -- the ONE load-bearing artifact of
        #      the whole run -- was never committed at all, while whatever
        #      uncommitted edits the input happened to be carrying were.
        #   2. `/handoff`'s DEFAULT shape (artifact-path omitted, so
        #      `_resolve_held_handoff_for_session` supplies the qualified
        #      predecessor path) ended in an UNCAUGHT `RuntimeError` out of
        #      `apply_base.scoped_commit`: d6 had already `git mv`'d the
        #      predecessor into `archive/handoffs/` and committed that, so
        #      `git add -- <predecessor>` came back rc=128 "did not match any
        #      files". Every directive had landed, d6's own commit was in
        #      history, and the operator got a traceback and an uncommitted
        #      baton -- which is precisely the "hand-commit the rest yourself"
        #      residue the replay work exists to end.
        #
        # `lineage["output_path"]` is where d1 scaffolds (and d3/d6 read as the
        # successor), so it is the only correct answer for both the pathspec and
        # the commit message's basename. Falls back to the prior value when a
        # decision object carries no lineage -- a malformed-envelope guard that
        # must not crash (see `test_apply_with_malformed_artifact_key_falls_back_
        # to_raw_artifact_path`).
        lineage_meta = artifact_meta.get("lineage") if isinstance(artifact_meta, dict) else None
        committed_artifact_path = (
            lineage_meta.get("output_path") if isinstance(lineage_meta, dict) else None
        ) or effective_artifact_path
        basename = Path(committed_artifact_path).name if committed_artifact_path else ""

        # d1's own compensator (`_D1_COMPENSATORS`, wired into
        # `apply_base.execute_directives` via `_execute_directives` above)
        # handles a partial-mutation abort where d1 landed but a LATER
        # directive in this same run failed -- see `_compensate_d1_scaffold`'s
        # own docstring; no separate call site is needed here.
        exit_code, report = _execute_directives(
            directives, judgment_points, root, decisions=decisions or {}
        )

        # Present unconditionally, including as [] -- same reasoning as
        # `commits` below.
        report["replayed"] = _collect_replayed_directives(directives, report)
        _report_replay_to_stderr(report["replayed"])

        # Present unconditionally, including as [] -- same reasoning as
        # `replayed` above and `commits` below; see `_collect_degraded`.
        report["degraded"] = _collect_degraded(report)

        commits = _collect_directive_commits(report)
        if exit_code in (APPLY_EXIT_OK, apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT):
            report["commit_sha"] = _scoped_commit(
                root, committed_artifact_path, kind, basename, report.get("landed", [])
            )
            if report["commit_sha"]:
                commits.append(
                    {
                        "directive_id": None,
                        "committed_by": "baton-assemble apply (_scoped_commit)",
                        "what": f"the {kind} artifact {committed_artifact_path}",
                        "sha": report["commit_sha"],
                    }
                )
        # Present unconditionally, including as [] -- an absent key would leave
        # "this run committed nothing" and "this report does not say" the same
        # read, which is the ambiguity this field exists to remove.
        report["commits"] = commits

        return _finalize_report(exit_code, report)


_USAGE_LINE = (
    "usage: {prog} apply <kind> [artifact-path] [--session-id <id>] "
    "[--decisions <json>] [--title <text>]\n"
    "       --decisions is a JSON object: {{\"<jp-id>\": {{\"disposition\": \"<value>\", ...}}}}\n"
    "       (\"value\" is accepted as an exact equivalent of \"disposition\" -- brief's own\n"
    "        output uses that key). Legal <value>s for a given jp-id are that judgment\n"
    "        point's own dispositions[].value entries from this run's `brief` output."
)


def _usage(prog: str) -> int:
    print(_USAGE_LINE.format(prog=prog), file=sys.stderr)
    return APPLY_EXIT_TRANSPORT_FAIL


def _usage_help(prog: str) -> int:
    """`--help`/`-h` request at ANY parse point in `apply`'s CLI surface --
    prints the usage line to stdout (the conventional --help stream,
    unlike `_usage`'s stderr for a genuine error) and returns
    `APPLY_EXIT_OK` (0), never a usage-error exit.

    Must be checked in `main_apply` BEFORE any positional token is treated
    as `kind`/`artifact_path` -- this closes a reproduced live break:
    `baton-assemble apply handoff --help` used to consume `"--help"`
    straight into `kind, artifact_path = argv[0], argv[1]` with zero
    validation, then ran `apply()` for real, scaffolding a junk handoff
    (`state/handoffs/<date>---help.md`) onto disk before anything
    downstream noticed. `main_apply` now checks every slot `--help`/`-h`
    could land in (bare, the `kind` slot via the leading-`-` flag-shape
    guard, the `artifact_path` slot, and the `tail` flags) before any
    positional value reaches `apply()`."""
    print(_USAGE_LINE.format(prog=prog))
    return APPLY_EXIT_OK


def main_apply(argv: list[str]) -> int:
    # --help/-h checked FIRST, before any positional consumption -- see
    # `_usage_help`'s own docstring for the live break this ordering closes.
    if argv and argv[0] in ("--help", "-h"):
        return _usage_help("baton-assemble apply")

    if not argv:
        return _usage("baton-assemble apply")

    # artifact-path is OPTIONAL for kind="handoff" -- omitted, `brief()`
    # self-resolves the baton this session actually holds from the claim
    # ledger, which is the whole point of the self-resolution seam: requiring
    # the operator to name a path they must first go look up is the guesswork
    # it exists to remove. Mirrors `main()`'s brief-arm parsing exactly; the
    # `apply` arm is the one an operator actually runs, so leaving it
    # mandatory-only would have left the seam unreachable in practice.
    # kind="spinoff" still requires the positional -- its own lineage
    # resolution reads the origin artifact the caller names.
    kind = argv[0]
    _rest = argv[1:]
    if kind == "handoff" and (not _rest or _rest[0].startswith("-")):
        artifact_path = ""
        tail = _rest
    else:
        if len(argv) < 2:
            return _usage("baton-assemble apply")
        artifact_path = argv[1]
        tail = argv[2:]

    # Reject a flag-shaped token wherever a slug/kind is expected -- never
    # silently accept it as a name. Checked BEFORE any repo resolution or
    # directive dispatch, so a mistyped/unsupported flag can never reach
    # `apply()` and mutate disk. `artifact_path`'s `--help`/`-h` case is
    # checked separately (help, not error) ahead of the general flag guard.
    if kind.startswith("-"):
        print(
            f"baton-assemble apply: kind must not look like a flag: {kind!r}",
            file=sys.stderr,
        )
        return APPLY_EXIT_TRANSPORT_FAIL
    if artifact_path in ("--help", "-h"):
        return _usage_help("baton-assemble apply")
    if artifact_path.startswith("-"):
        print(
            f"baton-assemble apply: artifact-path must not look like a flag: {artifact_path!r}",
            file=sys.stderr,
        )
        return APPLY_EXIT_TRANSPORT_FAIL

    if any(tok in ("--help", "-h") for tok in tail):
        return _usage_help("baton-assemble apply")

    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    title: Optional[str] = None
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--session-id":
            if i + 1 >= len(tail):
                return _usage("baton-assemble")
            session_id = tail[i + 1]
            i += 2
        elif tok == "--decisions":
            if i + 1 >= len(tail):
                return _usage("baton-assemble")
            try:
                decisions = json.loads(tail[i + 1])
            except json.JSONDecodeError as exc:
                print(f"baton-assemble apply: malformed --decisions JSON: {exc}", file=sys.stderr)
                return APPLY_EXIT_TRANSPORT_FAIL
            shape_error = validate_decisions_shape(decisions)
            if shape_error is not None:
                print(f"baton-assemble apply: {shape_error}", file=sys.stderr)
                return APPLY_EXIT_TRANSPORT_FAIL
            i += 2
        elif tok == "--title":
            if i + 1 >= len(tail):
                return _usage("baton-assemble")
            title = tail[i + 1]
            i += 2
        elif not tok.startswith("-") and title is None and artifact_path:
            # Unambiguous position: once artifact-path and --title are the
            # only two positional/flag slots left, a bare non-flag token
            # here can only be the title. Mirrors `main()`'s brief-arm
            # parsing (`baton_assemble/__init__.py`) -- see that arm's
            # comment for the round-trip this removes.
            title = tok
            i += 1
        elif not tok.startswith("-") and title is None:
            # artifact-path is optional for kind=handoff and was NOT bound
            # from a positional here -- genuinely ambiguous, stays an
            # error, but offers the fix (design-as-offers).
            print(
                f"baton-assemble apply: unrecognized argument {tok!r} — did you "
                f"mean --title {tok!r}?",
                file=sys.stderr,
            )
            return APPLY_EXIT_TRANSPORT_FAIL
        else:
            print(f"baton-assemble apply: unrecognized argument {tok!r}", file=sys.stderr)
            return APPLY_EXIT_TRANSPORT_FAIL

    exit_code, report = apply(kind, artifact_path, session_id=session_id, decisions=decisions, title=title)
    print(json.dumps(report, indent=2))
    return exit_code
