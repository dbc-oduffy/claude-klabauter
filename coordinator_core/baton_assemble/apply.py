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

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
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
from datetime import datetime, timezone
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

# `handoff.transition` (verb="claim") is the single writer d6's ledger
# reconcile delegates its re-stamp to (`_reconcile_claim_from_ledger`). The
# import is the REGISTRATION TRIGGER, not decoration: `_invoke_op_in_process`
# resolves handlers out of `coordinator_core.ipc`'s registry, which is empty
# for an op whose module was never imported -- the exact defect that surfaced
# once as `unrecognized op 'handoff.stamp_phase'` (see this module's d3 note).
import coordinator_core.ops.handoff_transition  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

_NO_CONSOLE = no_console_creationflags()

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
    cli = str(Path(claude_klabauter_bin) / "coordinator-doc-new.py")
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
    cli = str(Path(claude_klabauter_bin) / "session-claim-cli.py")
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
    doe-claude-em, 2026-07-29 cross-repo memo."""
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


class _CarryGatePredecessorUnreadable(RuntimeError):
    """Raised by `_dispatch_handoff_carry_gate` when the predecessor handoff
    path is missing, unreadable, or does not carry parseable frontmatter --
    DISTINCT from `CarryGateError`/a gate refusal, so a caller inspecting the
    exception class (or the CLI's exit contract this module composes) can
    tell "the predecessor is unreadable" from "the predecessor's carried
    items declare undeclared state" without string-matching a message."""


def _dispatch_handoff_carry_gate(args: list[str], repo_root: Path) -> dict[str, Any]:
    """kind=handoff's d7: the disposition gate for the PREDECESSOR's own
    `## Carried Forward` items, run INSIDE `apply()`'s transaction rather
    than hand-invoked from `/handoff`'s SKILL.md prose -- see this module's
    own plan backlink (docs/plans/2026-08-10-the-carry-gate-nobody-put-
    inside-the-transaction.md) for why "the operator remembers to run a CLI"
    is the exact shape this fix exists to remove.

    Reads the predecessor's `carried_items` frontmatter array directly and
    calls `coordinator_core.ops.handoff_carry_gate.evaluate_gate` IN-PROCESS
    -- `handoff_carry_gate` is deliberately NOT a registered op (no
    `handoff.carry_gate` entry in `_registry_map.py`/`op_scopes.py`/
    `authz/classification.py`), so `_invoke_op_in_process` is not the seam
    here.

    FAIL POSTURE (models `_dispatch_handoff_stamp_phase` above): a non-ok
    `GateResult` RAISES -- `apply_base.execute_directives` treats only a
    raised exception as failure, and `evaluate_gate`'s own return value is an
    ordinary value that would otherwise land in `landed` under a green
    `APPLY_EXIT_OK` (the exact defect `_dispatch_handoff_author_fork` and
    `_dispatch_handoff_stamp_phase` were each fixed for). The gate's own
    per-item violation lines are preserved VERBATIM in the raised message --
    they already name the `carry_id` and the concrete reason, so re-wording
    them destroys the actionable part.

    A missing/unreadable predecessor file, or one with no parseable
    frontmatter, raises `_CarryGatePredecessorUnreadable` -- a DISTINCT error
    class from a gate refusal (`RuntimeError` carrying the gate's own
    `CarryGateError`), mirroring `coordinator/bin/handoff-carry-gate`'s own
    CLI exit contract (1 = refusal, 2 = malformed/unreadable/usage).

    Read/parse/validate of the predecessor's `carried_items` array is NOT
    reimplemented here -- it calls
    `coordinator_core.ops.handoff_carry_gate.read_carried_items` (the same
    read path the `handoff-carry-gate` CLI's own `main()` uses) and maps its
    `CarryGateError` onto `_CarryGatePredecessorUnreadable`, so a future
    malformed-input check added there is inherited automatically rather than
    drifting out of sync with a second copy. See Review:
    coordinatorcode-reviewer-625ab891 finding 1."""
    from coordinator_core.ops.handoff_carry_gate import CarryGateError, evaluate_gate, read_carried_items

    predecessor_path = args[0] if args else ""
    target = repo_root / predecessor_path
    try:
        items = read_carried_items(str(target))
    except CarryGateError as exc:
        raise _CarryGatePredecessorUnreadable(
            f"handoff-carry-gate: {predecessor_path!r} at {target}: {exc}"
        ) from exc
    except OSError as exc:
        raise _CarryGatePredecessorUnreadable(
            f"handoff-carry-gate: could not read predecessor {predecessor_path!r} "
            f"at {target}: {exc}"
        ) from exc

    result = evaluate_gate(items)
    if not result.ok:
        raise RuntimeError(
            "handoff-carry-gate REFUSED -- "
            f"{predecessor_path!r} carries carried_items with undeclared "
            "state:\n" + "\n".join(result.violations)
        )
    return {"cli": "handoff-carry-gate", "args": args, "result": {"ok": True}}


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


def _ledger_claim_record(predecessor_path: str, repo_root: Path) -> Optional[dict[str, str]]:
    """The durable, PREDECESSOR-SIDE claim record for `predecessor_path`, read
    off the branch-independent claim ledger
    (`<common_dir>/coordinator-sessions/handoff-claims/<basename>/`), or None
    when no such record exists.

    This is a SECOND, independent evidence source for DR-242's "was this baton
    ever claimed?" question, and it is emphatically NOT the successor-named-child
    evidence DR-242 § 2 forbids: the ledger entry is written only by
    `coordinator_core.session.claims.claim_artifact` at the instant a session
    takes the baton, names the baton by its own basename, and knows nothing
    about any successor. It is also the SAME ledger `_resolve_held_handoff_for_
    session` reads to NAME this predecessor in the first place -- so consulting
    it here closes a split-brain in which the resolver and the DR-242 gate read
    two different halves of one claim.

    Delegates to `coordinator_core.claim_state.resolve_historical_claim`
    (2026-08-07, claim-state-ledger-first-authoritative-read plan, C1) -- this
    function's own ledger-only read was the reference implementation that
    module's accessors were generalized FROM; the shared core now lives there
    and this is the sole remaining call site re-shaped around its return.

    WHY THE LIVENESS-FREE ACCESSOR AND NOT `resolve_claim_state`. The two answer
    different questions, and the C1 re-shape initially bound this call site to
    the wrong one. `resolve_claim_state` answers "who holds this baton NOW", so
    it gates the ledger record on `cs_claim_holder_live` and a dead holder
    correctly degrades to "no ledger claim" -- correct for pickup-eligibility,
    ownership, and reaping, and wrong here. DR-242's question at this gate is
    retrospective: "was this baton ever claimed", where the holder's liveness is
    irrelevant, because the ONLY predecessor that ever reaches a supersede is one
    a session claimed, worked, and finished with. Gating on liveness therefore
    made the reconcile unreachable in exactly the incident
    `_reconcile_claim_from_ledger` exists to fix -- the ledger held the claim, the
    holder had exited, and d6 degraded `predecessor-not-claimed-or-shipped`
    anyway. `handoff_archive_transition._attribute_claim_holder` -- the writer on
    the far side of this same supersede -- already reads the ledger liveness-free
    for the identical reason; see `_supersede_continued`'s "Superseding is
    retrospective" paragraph. This is not a weakening of the liveness gate: that
    gate stays untouched for every decision it belongs to.

    Negative-spec: does NOT consult the frontmatter mirror. This function's
    contract is ledger-only -- the mirror is precisely the half d6's own gate has
    already read and found empty, so re-reading it here would answer nothing.

    REACHABILITY (2026-08-13, code-review Slice-3 P2 follow-up) -- the prose
    invariant above ("the only predecessor that ever reaches a supersede is
    one a session claimed, worked, and exited") is now STRUCTURALLY
    enforced, not merely asserted, and this paragraph names the enforcing
    mechanism rather than re-asserting the prose.

    The reviewer's concern was: a predecessor whose true ledger holder is a
    DIFFERENT session that is STILL LIVE reaches this liveness-free read via
    `resolve_lineage`'s `kind="handoff"` explicit-`artifact_path` route (the
    `is_own_handoff_record` branch sets `lineage["predecessor"]` to whatever
    path the caller supplies, UNCONDITIONALLY -- unlike the empty-
    `artifact_path` self-resolution path, `_resolve_held_handoff_for_
    session`, which filters to the caller's own ledger entries). That route
    genuinely reaches `_dispatch_handoff_supersede_predecessor` with a
    foreign predecessor path -- but verified BY EXECUTION (two probes, not
    inference; see this module's own test coverage in
    `coordinator_core/baton_assemble/tests/test_ledger_claim_record_liveness.py`),
    it does NOT reach past this function's caller into the liveness-free
    read for a genuinely-live different holder: `_dispatch_handoff_
    supersede_predecessor`'s own DR-242 gate (`archival.
    claimed_or_shipped_at_path`) is checked FIRST and already consults
    `claim_state.resolve_claim_state` -- the LIVENESS-GATED sibling accessor
    (`cs_claim_holder_live`) -- which reports a live ledger claim as
    claimed-or-shipped on its own, before `_reconcile_claim_from_ledger`
    (and therefore this liveness-free function) is ever called at all. Probe
    1 (a ledger record for a non-existent, hence naturally-dead, session id)
    reached this function and reconciled, matching the documented dead-
    holder incident. Probe 2 -- the identical fixture with
    `coordinator_core.session.liveness.claim_holder_live` forced to report
    the SAME record's holder as live -- found `claimed_or_shipped_at_path`
    already `True` before `_reconcile_claim_from_ledger` was ever reached;
    this function's own print statement never fired, and the op composed
    directly off the DR-242 gate's own live-claim read, never touching this
    liveness-free accessor. The enforcing mechanism this paragraph names is
    therefore `_dispatch_handoff_supersede_predecessor`'s ORDERING: DR-242's
    gate always runs first, and it is liveness-aware; this liveness-free
    accessor is reachable ONLY on the branch where that gate already found
    no live claim -- structurally the same "claimed, worked, exited" shape
    the retired prose asserted, now enforced by that gate's own call order
    rather than merely believed.
    """
    from coordinator_core.claim_state import resolve_historical_claim

    record = resolve_historical_claim(Path(predecessor_path), repo_root=repo_root)
    if record is None:
        return None
    session_id, claimed_at = record
    return {
        "session_id": session_id,
        "claimed_at": claimed_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _reconcile_claim_from_ledger(
    predecessor_path: str, repo_root: Path
) -> tuple[bool, str | None]:
    """Re-stamps a predecessor's claim frontmatter FROM the durable claim ledger
    when the two have desynchronized, and reports whether the record now
    satisfies DR-242. Returns `(satisfied, rejection_error)`: `rejection_error`
    is `None` unless the frontmatter re-stamp itself was attempted and refused
    by schema validation, in which case it carries the op's own `error` text so
    the caller can attribute the refusal correctly instead of folding it into
    the genuine never-claimed case. `(False, None)` leaves d6's refusal exactly
    as it was. When the re-stamp was attempted and refused but the op returned
    no `error` text, `rejection_error` is `""` (empty string), not `None` --
    deliberate: the caller's `is not None` test must still route to the
    validation-rejected path, not fold this case into the never-claimed one.

    THE DEFECT THIS CLOSES (live repro, claude-klabauter 2026-08-07). The claim
    ledger lives under `.git/` -- branch-independent and shared by every
    worktree -- while its frontmatter mirror (`status: claimed` / `claimed_by` /
    `claimed_at`) is a TRACKED FILE, and therefore branch-dependent. A session
    claimed a baton on branch `two-tier-engine-root` (commit 11fe08d51 stamped
    the frontmatter); the tree was later on `main`, which never carried that
    commit, so the on-disk record read `status: open` / `ready_to_fire` with no
    claim fields while the ledger still held the claim. `brief()` resolved the
    predecessor FROM the ledger and armed d6; d6's DR-242 gate then read the
    frontmatter half and declined `predecessor-not-claimed-or-shipped`. Net
    effect: a fully-worked baton left advertising `pickup_ready: true`, with the
    manual `handoff-archive-transition supersede` route refusing for the same
    reason, so the only remaining remedy was hand-editing frontmatter -- exactly
    the undischarged rule this repo's north star forbids.

    WHY THIS IS NOT A WEAKENING OF DR-242, and why it is not a `--force`. No
    evidence is invented and no operator assertion is accepted: the claim is
    re-derived from a durable artifact that records the claim event itself, and
    the re-stamp is delegated to the existing single writer for that transition
    (`handoff.transition` verb="claim"), never hand-rolled here. If the ledger
    holds nothing, this returns False and the refusal stands verbatim -- an
    unclaimed predecessor is still never superseded, by this path or any other.
    The op's own choke point (`handoff_archive_transition`'s supersede block)
    re-reads the frontmatter afterwards and is deliberately left untouched, so
    the reconcile has to genuinely succeed to change any outcome.
    """
    record = _ledger_claim_record(predecessor_path, repo_root)
    if record is None:
        return False, None

    from coordinator_core.archival import claimed_or_shipped_at_path

    result = _invoke_op_in_process(
        "handoff.transition",
        {
            "verb": "claim",
            "handoff_path": predecessor_path,
            "session_id": record["session_id"],
            "at": record["claimed_at"],
        },
        repo_root,
    )
    if result.get("exit_code") != 0:
        error_text = result.get("error")
        print(
            "baton-assemble apply: handoff.supersede_predecessor -- the durable "
            f"claim ledger holds a claim on {predecessor_path!r} (session "
            f"{record['session_id']}) but the frontmatter re-stamp failed "
            f"({error_text!r}); DR-242's refusal stands.",
            file=sys.stderr,
        )
        return False, str(error_text) if error_text is not None else ""
    if not claimed_or_shipped_at_path(str(repo_root / predecessor_path)):
        return False, None
    print(
        "baton-assemble apply: handoff.supersede_predecessor reconciled "
        f"{predecessor_path!r} from the durable claim ledger -- its frontmatter "
        "carried no claim (a branch that never received the claiming commit, or "
        f"a discarded working-tree edit) while the ledger recorded session "
        f"{record['session_id']} holding it since {record['claimed_at']}. The "
        "claim was re-stamped from that record and the succession proceeds.",
        file=sys.stderr,
    )
    return True, None


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
    the same "degrade, don't raise, on a genuinely not-applicable case"
    posture other dispatchers in this module draw: the op
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

    reconcile_error: str | None = None
    if not claimed_or_shipped_at_path(str(repo_root / predecessor_path)):
        reconciled, reconcile_error = _reconcile_claim_from_ledger(
            predecessor_path, repo_root
        )
    else:
        reconciled = True
    if not reconciled and reconcile_error is not None:
        print(
            # Review: code-reviewer -- one fact, stated once, per
            # docs/wiki/guard-messaging.md § Register: this predecessor WAS
            # claimed, and the frontmatter re-stamp was refused by validation.
            "baton-assemble apply: handoff.supersede_predecessor degraded -- "
            f"{predecessor_path!r} was claimed per the durable claim ledger, "
            f"but its frontmatter re-stamp was refused by schema validation "
            f"({reconcile_error!r}); DR-242's refusal stands.",
            file=sys.stderr,
        )
        return {
            "cli": "handoff.supersede_predecessor",
            "args": args,
            "result": None,
            "degraded": {
                "reason": "predecessor-claim-restamp-validation-rejected",
                "predecessor": predecessor_path,
                "error": reconcile_error,
            },
        }
    if not reconciled:
        print(
            "baton-assemble apply: handoff.supersede_predecessor degraded -- "
            f"{predecessor_path!r} was never claimed or shipped -- neither its own "
            "frontmatter nor the durable handoff-claims ledger carries a claim "
            "record -- so DR-242 leaves it nothing to supersede (a successor-named "
            "child is not evidence of succession). The predecessor was left exactly "
            "as it was; the mint and every other directive in this run proceeded "
            "normally. If that predecessor SHOULD have been superseded, claim it and "
            "re-run -- do not hand-stamp the succession.",
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
    "handoff.supersede_predecessor": _dispatch_handoff_supersede_predecessor,
    "handoff-carry-gate": _dispatch_handoff_carry_gate,
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

    script_path = _resolve_claude_klabauter_bin() / "coordinator-doc-new.py"
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
    state, no I/O (see `coordinator/bin/coordinator-doc-new.py`'s
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
    convention, see `_dispatch_handoff_author_fork`'s degrade prints) naming
    the file and the exception, and STILL declines -- the fail-safe direction is unchanged,
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

    2026-08-03. Handlers here degrade rather than raise (d3's
    `handoff.author_fork` stamp-mode field degrades, and d6's DR-242 gate as
    of this same fix; d4's now-retired `render-project-tracker` handler also
    degraded), and a
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
    print). stdout stays the machine
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
    from coordinator_core.pickup_assemble import compute_repo_identity_gate  # C3: foreign-repo gate

    repo_root_was_cwd_derived = repo_root is None
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

    # C3 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md):
    # the C1 foreign-repo gate. `apply()` always passes an explicit `root` to
    # `brief()` below (it must, to keep both computing off the SAME resolved
    # root) -- so brief()'s own cwd-derived refusal check never fires from
    # this call chain, and the refusal decision has to be made HERE, against
    # apply()'s own `repo_root` parameter, mirroring the `root = repo_root or
    # resolve_repo_root()` gating shape used at every other site in this plan.
    # brief() still independently records the SAME verdict into its own
    # envelope's `gates["repo_identity"]` (see baton_assemble.brief) -- this
    # call is only for the refusal decision, not a second source of truth.
    repo_identity_gate = compute_repo_identity_gate(root, resolved_sid)
    if repo_root_was_cwd_derived and repo_identity_gate["verdict"] == "MISMATCH":
        return _finalize_report(APPLY_EXIT_TRANSPORT_FAIL, {"error": repo_identity_gate["message"]})

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

        # C3 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md):
        # the MATCH/UNRESOLVED verdict this run's own gate above computed --
        # states plainly the check could not run (UNRESOLVED) rather than
        # implying it passed, mirroring `build_envelope`'s `gates["repo_
        # identity"]` shape used by `brief()`'s own envelope.
        report["gates"] = {"repo_identity": repo_identity_gate}

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
