"""
coordinator_core.pickup_assemble.apply — the `pickup-assemble apply` computed-
skill engine: the MUTATING half `brief()`'s read-only decision object hands off
to (module docstring § "the compute/apply split").

Purpose: recomputes the brief in-process from an artifact path (never trusts a
caller-supplied decision object — see negative-spec below), halts at any
unresolved judgment point or a denied claim, and otherwise executes the
brief's `directives[]` through a CLOSED, literal dispatch table. Pins its own
exit-code contract (0-4) — locally scoped to the mutating half, NOT inherited
from `brief`'s 0/1/2/3 (contract § "Exit-code contract").

`drop()` is the clean full inverse of a granted claim (§ "Dropping the baton is a
first-class exit"): composes `claims.release_artifact` + `archive_stamp.
cs_unclaim_handoff` into the ONE thing an operator holding an unwanted baton
wants to express — back to `open` + `ready_to_fire`, claim record wiped, as if
the pickup never happened. Both composed primitives are unconditional and
idempotent (a no-op release on a not-yet-claimed artifact; a no-op unclaim on
a not-yet-claimed handoff) — `drop` never inspects which of `apply`'s
directives actually landed before composing them (AC9g's partial-mutation
recovery: a half-applied artifact still returns cleanly).

The hold-path residue (§ "The hold path is the residue"): `apply` reads
dispositions from the session-scoped decision-object file the auto-fire hook
(chunk C3) writes, rather than requiring a `--decisions` JSON blob be recalled
and retyped — the EM fills a blank in a file it is already holding.
`--decisions` (this module's own `apply(decisions=...)` parameter / CLI flag)
survives only as the crash-resume/audit surface (the Director of Engineering review, F6); the file is
the primary path.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-bz-rebuild-pickup-to-embody-th-037313, chunks C2a + C2b + C2c + C2d

Security-load-bearing (the Director of Engineering review F1 / the Staff Engineer second-pass finding #1, AC9e):
the executable universe this module can reach is a CLOSED CONSTRUCTION, not an
asserted property. `directives[].cli` resolves through `_CLI_DISPATCH` — a
literal, hardcoded `dict[str, Callable]` — never `getattr`, never
`importlib.import_module` on a brief-derived name, never a subprocess/shell
invocation built from `directives[].args`. An unrecognized `cli` (or an
unrecognized verb within a recognized `cli`'s own closed verb set) raises
before any directive in the run executes — "mutates nothing" means the WHOLE
run aborts pre-validation, not merely the one bad directive. The one CLI whose
composed primitives shell out (`archive-stamp-cli` -> `cs_claim_handoff` ->
`_run_git`) only ever constructs a literal `["git", ...]` argv; no element of
any `directives[].args` value is ever concatenated into that argv.
(`cs_consume_handoff` is the pre-rename name; it survives only as a
deprecated alias of `cs_claim_handoff`, not a second code path.)

`directives[].already_satisfied` (e.g. a self-held claim) is skipped, not
re-run — a second `apply` on a self-held artifact is a clean no-op, not a
failure. `directives[].depends_on` orders execution — a directive never
dispatches before the directive id(s) it names.

AMENDMENT 2026-07-24 (chunk C2, B4 baton — DR-092 un-defer): this module's
directive-execution engine, dependency ordering, CLI-resolution seam,
session-identity propagation, in-repo path safety, and scoped-commit
discipline are now COMPOSED from `coordinator_core.contract.apply_base`
(the second real apply/dispatch half — B4's `baton_assemble` — is the
named trigger that un-defers the shared runner per DR-092) rather than
implemented locally. This module keeps its own closed `_CLI_DISPATCH`
table, its handler bodies, its `_run_git` in-process read-model, and the
`apply()`/`drop()` orchestration (brief-recompute, claim-grant
resolution, artifact classification, the session-scoped decision file) —
exactly the pieces that are pickup-specific, not shared. The names below
(`_resolve_cli`, `_execute_directives`, `_scoped_commit`, etc.) are thin
wrappers binding apply_base's generic implementation to this module's own
dispatch table / commit-message shape / git runner, preserving every
existing call signature.

AMENDMENT 2026-07-24 (chunk C7 Part B — supersedes the paragraph this
replaces): the judgment halt is PER-DIRECTIVE, not a blunt "any non-empty
`judgment_points` stops the whole run dead before anything executes" rule.
A directive fires when its own `depends_on` is `None`, or names a
judgment-point id resolved (via the session-scoped `decisions` map) to a
disposition whose OWN `resolves` list names that directive — never merely
"some disposition was picked" (`apply_base.disposition_resolves_directive`,
The Director of Engineering v2 finding-1 predicate). A directive whose dependency is unresolved,
or resolved to a disposition that does not name it, is skipped this pass;
every OTHER directive that IS ready still dispatches. The run reports
`APPLY_EXIT_HALTED_AT_JUDGMENT` whenever at least one directive was blocked
this way — see `_execute_directives`/`apply_base.directive_gate_open`.

`claim_grant` is re-resolved immediately before mutating, never trusted from
the brief-time value, and this re-resolution is UNCONDITIONAL on
`judgment_points`' contents — it is the pre-loop blanket gate every
directive (including a `depends_on: None` one) dispatches behind, not a
judgment point itself. As of this amendment the liveness stand-down
judgment point (`j1`) no longer carries `revalidate_at_dispatch: true` — its
`compute_liveness_signal` input is now a durable committed frontmatter
stamp, stable across the brief-to-apply gap, so a recorded `proceed`
disposition on it is honored rather than discarded (see
`pickup_assemble.__init__.compute_liveness_signal`'s docstring). `claim_grant`
remains the one revalidating check this module performs at dispatch time —
AC9f's freshness discipline is expressed directly in `apply()`'s
`_resolve_claim_grant` closure rather than via a `revalidate_at_dispatch`
flag on a judgment point.

Negative-spec:
    - Do NOT add a dispatch entry that resolves `cli` via `getattr`,
      `importlib`, or any brief-derived string — every entry in
      `_CLI_DISPATCH` is a literal key written by hand in this file.
    - Do NOT call `subprocess.run`/`Popen` directly in this module for a
      brief-derived command — the one subprocess path this module's composed
      primitives reach (`_run_git`, inside `archive_stamp.py`) never receives
      `directives[].args` as argv elements.
    - Do NOT trust a caller-supplied decision object as the mutation plan —
      `apply()` recomputes `brief()` itself from `artifact_path`; there is no
      seam here that accepts an externally-authored `directives[]` list.
    - Do NOT auto-resolve a `judgment_points` entry, with or without a
      `recommendation` present (AC5c/AC5d) — `recommendation` is never read
      anywhere in this module's control flow, and a directive only ever
      fires off an EXPLICIT `decisions[jp_id].disposition` whose OWN
      `resolves` list names it (chunk C7 Part B); an unresolved (or
      non-terminally-resolved) judgment point blocks every directive that
      names it in `depends_on`, never auto-fires one.
    - Do NOT re-run an `already_satisfied` directive — its handler is never
      called; it is reported landed without dispatch.
    - Do NOT read a composed primitive's return value directly as a bool —
      `claims.claim_artifact` returns `bool` (`True` == success) while
      `archive_stamp.cs_claim_handoff`/`coordinator-tasks-mirror.cmd_init`
      return a POSIX-style `int` exit code (`0` == success); an `int` read as
      a bare truthy value inverts a successful `0` into falsy. Route every
      primitive's return through `_normalize_primitive_result`.
    - Do NOT stage or commit with `git add -A`/`git add .`/a bare `git
      commit` (AC10) — `apply` runs against a shared concurrent-EM working
      tree that may already carry a sibling session's own files staged; every
      `git add`/`git commit` this module issues names its one resolved
      artifact path as an explicit pathspec, never the whole tree.
    - Do NOT make `drop` inspect which `apply` directives landed before
      composing its two primitives (AC9g) — that is the state machine this
      module is instructed not to build; `drop` always calls
      `release_artifact` then, for a handoff, `cs_unclaim_handoff`,
      trusting each primitive's own idempotency.
    - Do NOT treat a corrupt or absent session-scoped decision file as fatal
      — `apply` degrades to an empty dispositions map (the coast-clear/
      judgment-halting behaviour it already has), never raises, on any read
      failure of that file.
    - Do NOT pass `_scoped_commit`/`git add`/`git commit` anything but the
      RESOLVED `artifact["path"]` `brief()`'s own `resolve_artifact` computed
      (2026-07-26 defect fix) — never re-derive a path from the raw,
      possibly-bare-basename `artifact_path` argument a second time in this
      module; there is exactly one resolver
      (`pickup_assemble.resolve_artifact`) and `apply()` threads its output
      through unchanged.
    - Do NOT let `APPLY_EXIT_OK`/`landed: []` be reachable when the caller
      supplied a non-empty `decisions` payload (via `--decisions` or the
      session-scoped decision file) that resolved to NO directive and NO
      judgment point to attach to (2026-07-26 defect fix) — a genuinely
      terminal artifact (archived, or an already-`actioned` memo) must
      fail loud naming what was discarded, never silently report success
      while dropping the caller's fields on the floor.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.archive_stamp import (
    cs_action_memo,
    cs_claim_handoff,
    cs_claim_memo_stamp,
    cs_gate_recheck_handoff,
    cs_release_memo_revert,
    cs_unclaim_handoff,
)
from coordinator_core.claim_state import handoff_claim_dir
from coordinator_core.contract import apply_base
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.lifecycle import git_common_dir
from coordinator_core.locked_write import locked_rmw
from coordinator_core.pickup_assemble import (
    DISPOSITION_CONTENT_KEYS,
    EXIT_OK as _BRIEF_EXIT_OK,
    _ArtifactUnreadable,
    _run_git,
    brief,
    compute_claim_grant,
    resolve_artifact,
    split_artifact_args,
    validate_decisions_shape,
)
from coordinator_core.session import liveness as _liveness
from coordinator_core.session.claims import (
    CLAIM_STAGE_APPLY,
    CLAIM_STAGE_BRIEF,
    claim_artifact,
    claim_stage,
    claim_stamped,
    demote_claim_stage_to_brief,
    mark_claim_stamped,
    promote_claim_stage,
    release_artifact,
)
from coordinator_core.session.core import in_warm_served_request
from coordinator_core.telemetry.composition_record import (
    flush_composition_record,
    make_fleet_budget,
)

# ---------------------------------------------------------------------------
# Exit-code contract (AC9g, the Staff Engineer second-pass finding #7) — composed from
# apply_base, shared by every apply/dispatch half. NOT inherited from
# `brief`'s 0/1/2/3 — the two halves define their own contracts per
# `computed-skills.md` § Exit-code contract.
# ---------------------------------------------------------------------------
APPLY_EXIT_OK = apply_base.APPLY_EXIT_OK
APPLY_EXIT_HALTED_AT_JUDGMENT = apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
APPLY_EXIT_CLAIM_DENIED = apply_base.APPLY_EXIT_CLAIM_DENIED
APPLY_EXIT_TRANSPORT_FAIL = apply_base.APPLY_EXIT_TRANSPORT_FAIL
APPLY_EXIT_PARTIAL_MUTATION = apply_base.APPLY_EXIT_PARTIAL_MUTATION

# Env vars an explicit `--session-id` propagates into (AC9(a), the Director of Engineering F3): the
# composed primitives this module calls resolve identity through TWO
# independently-tiered chains that do not share an env var —
# `coordinator_core.session.core.resolve_session_id` (tier 1
# `COORDINATOR_SESSION_ID`, used by `claim_artifact`/`compute_claim_grant`) and
# `coordinator_core.ops.session_context.resolve_current_session_id` (tier 1
# `CLAUDE_SESSION_ID`, used by `cs_claim_handoff`). Setting both pins every
# composed primitive to the SAME explicit identity for the duration of a run,
# rather than letting either chain fall through to its own ambient tier-3/4
# sentinel file.
_SESSION_ENV_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID")

# Env vars read (never written) to resolve an implicit session id when
# `--session-id` is not passed — tiers 1-3 of both chains above, ordered
# highest-precedence first. The ambient tier-4 sentinel file is deliberately
# NOT in this list (AC9(a)): under concurrency ambiguity it returns empty or
# names an unrelated session, so `apply` never reads it.
_SESSION_ENV_READ_ORDER = (
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


# ---------------------------------------------------------------------------
# Generic directive-execution machinery (exceptions, `DirectiveResult`,
# dependency ordering, judgment-gate predicates, path-safety, dispatch
# resolution) is COMPOSED from `apply_base`, not reimplemented here — see the
# AMENDMENT 2026-07-24 (chunk C2) docstring paragraph above. Every name below
# is either a direct alias of the `apply_base` object (so
# `pa_apply.UnrecognizedDirective` and `apply_base.UnrecognizedDirective`
# are literally the same class — a handler raising the shared exception is
# still catchable/testable under this module's own name) or a thin wrapper
# that binds `apply_base`'s generic signature to this module's own
# dispatch table / commit-message shape / git runner, preserving every
# existing call signature this module's own tests and callers depend on.
# ---------------------------------------------------------------------------
UnrecognizedDirective = apply_base.UnrecognizedDirective
OutOfRepoPath = apply_base.OutOfRepoPath
NoResolvableSessionId = apply_base.NoResolvableSessionId
DirectiveDependencyCycle = apply_base.DirectiveDependencyCycle
DirectiveResult = apply_base.DirectiveResult

_normalize_primitive_result = apply_base.normalize_primitive_result
_assert_in_repo_root = apply_base.assert_in_repo_root
_reject_path_traversal = apply_base.reject_path_traversal
_judgment_points_by_id = apply_base.judgment_points_by_id
_directive_gate_open = apply_base.directive_gate_open


# ---------------------------------------------------------------------------
# `coordinator-tasks-mirror` — the one composed primitive that lives outside
# `coordinator_core` (a `coordinator/bin/` script, per DR-088's registration
# seam). Loaded once, by a fixed literal path resolved from THIS module's own
# location (never from the target artifact's `repo_root`, which may be a
# different repo entirely) — not a brief-derived import target.
# ---------------------------------------------------------------------------
_TASKS_MIRROR_SCRIPT = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-tasks-mirror.py"
)
_tasks_mirror_module = None


def _load_tasks_mirror_module():
    global _tasks_mirror_module
    if _tasks_mirror_module is not None:
        return _tasks_mirror_module
    spec = importlib.util.spec_from_file_location(
        "_coordinator_tasks_mirror_impl", _TASKS_MIRROR_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise UnrecognizedDirective(
            f"could not load coordinator-tasks-mirror from {_TASKS_MIRROR_SCRIPT}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _tasks_mirror_module = module
    return module


# ---------------------------------------------------------------------------
# Dispatch handlers — one per (cli, verb) pair the assembler ever emits.
# Every handler's own verb check is itself a literal comparison, never a
# lookup keyed by brief-derived data beyond the one closed comparison.
# ---------------------------------------------------------------------------

def _dispatch_session_claim_cli(args: list[str], repo_root: Path) -> dict[str, Any]:
    if not args or args[0] != "claim-artifact":
        raise UnrecognizedDirective(f"session-claim-cli: unrecognized verb {args[:1]!r}")
    if len(args) != 3:
        raise UnrecognizedDirective("session-claim-cli claim-artifact: expected 2 arguments")
    class_, basename = args[1], args[2]
    if class_ not in ("handoff", "memo"):
        raise UnrecognizedDirective(f"session-claim-cli: unrecognized class {class_!r}")
    basename = _reject_path_traversal(basename, label="session-claim-cli basename")
    ok = _normalize_primitive_result(claim_artifact(class_, basename, str(repo_root), cwd=str(repo_root)))
    if not ok:
        raise RuntimeError(f"session-claim-cli claim-artifact {class_} {basename}: claim failed")
    return {"cli": "session-claim-cli", "verb": "claim-artifact", "class": class_, "basename": basename}


def _restamp_execution_sha_mutate(computed_sha: str) -> Callable[[str], str]:
    """Returns the `locked_rmw` mutate closure for `restamp-execution-sha`
    (chunk C9) — replaces (or, if absent, inserts) `execution_authorized_sha`
    on the target plan/handoff's own frontmatter with `computed_sha`, via the
    same text-based frontmatter primitives every other lifecycle writer in
    this codebase uses (never pyyaml round-trip, so every OTHER field's byte
    shape is preserved verbatim). `numeric_quoting=True` mirrors
    `stamp-shipped-in`'s own SHA-as-int defence (a git SHA is occasionally
    all-digit) — see `frontmatter.primitives.serialize_yaml_scalar`.
    Raises `ValueError` (via `replace_fm_field`) if the existing value is a
    block-scalar — `locked_rmw` propagates that after releasing the lock,
    which `_dispatch_archive_stamp_cli` surfaces as a genuine handler failure
    rather than a silent corruption."""

    def _mutate(text: str) -> str:
        split = split_frontmatter(text)
        if split is None:
            raise ValueError("restamp-execution-sha: target file has no parseable frontmatter")
        if read_fm_field_unquoted(split.fm_text, "execution_authorized_sha") is None:
            fm = insert_fm_field(
                split.fm_text, "execution_authorized_sha", computed_sha, numeric_quoting=True
            )
        else:
            fm = replace_fm_field(
                split.fm_text, "execution_authorized_sha", computed_sha, numeric_quoting=True
            )
        return rebuild(split, fm)

    return _mutate


def _dispatch_archive_stamp_cli(args: list[str], repo_root: Path) -> dict[str, Any]:
    if not args:
        raise UnrecognizedDirective("archive-stamp-cli: unrecognized verb ()")
    verb = args[0]

    if verb in ("claim-handoff", "consume-handoff"):
        # "consume-handoff" accepted read-side so an in-flight pre-computed
        # decision object (emitted before this rename) does not break — this
        # module itself only ever EMITS the canonical "claim-handoff" below.
        if len(args) != 2:
            raise UnrecognizedDirective(f"archive-stamp-cli {verb}: expected 1 argument")
        handoff_path = _assert_in_repo_root(Path(args[1]), repo_root)
        # return_result=True (C2/2026-08-13) — surface the op's real error
        # (e.g. a `_HANDOFF_CROSS_FIELD_RULES` validation rejection) rather
        # than a generic "failed", same reasoning as claim-memo-stamp below.
        result = cs_claim_handoff(str(handoff_path), return_result=True)
        ok = _normalize_primitive_result(result["exit_code"])
        if not ok:
            raise RuntimeError(
                f"archive-stamp-cli {verb} {args[1]}: {result.get('error', 'failed')}"
            )
        # Durable stamp-landed marker (cross-repo/inbox/2026-08-13-doe-claude-
        # em-pickup-already-satisfied-masks-a-refused-write.md) — written ONLY
        # here, after `ok` has confirmed `cs_claim_handoff`'s underlying
        # `handoff_transition._claim` genuinely landed the frontmatter mutation
        # (its own post-write `_validate_fm` pass already happened inside that
        # call). Best-effort: a marker-write failure must never fail a
        # directive whose real work already succeeded.
        try:
            _common_dir = git_common_dir(repo_root)
        except Exception:
            _common_dir = None
        if _common_dir is not None:
            mark_claim_stamped(handoff_claim_dir(_common_dir, handoff_path))
        return {"cli": "archive-stamp-cli", "verb": "claim-handoff", "handoff_path": args[1]}

    if verb in ("gate-recheck", "gate-recheck-handoff"):
        # Piece A (cross-repo/inbox/2026-08-04-example-market-data-repo-em-
        # pickup-jgate-cleared-strands-gate-fields.md) — records the
        # `jgate: cleared` disposition BEFORE `claim-handoff` moves the
        # handoff past `awaiting_gate`, the one state both gate-repair
        # verbs (`gate-recheck`, `gate-cascade-clear`) require. Sequenced
        # strictly before `claim-handoff` via `directives[].depends_on`
        # (`build_gate_recheck_directive`'s id names both `jgate` and this
        # directive's own id in d2's depends_on — see that function's
        # docstring). `_assert_in_repo_root`-bound like `claim-handoff`
        # above; `cs_gate_recheck_handoff` itself has no `return_result`
        # kwarg (unlike `cs_claim_handoff`/`cs_claim_memo_stamp`) — it
        # already prints the real `handoff.transition` error to stderr on
        # failure, so this handler surfaces a directive-scoped RuntimeError
        # rather than inventing a second error-detail channel on a
        # composed primitive this module does not own (archive_stamp.py is
        # out of scope for this change).
        #
        # Both spellings are accepted. `build_gate_recheck_directive` now
        # emits `gate-recheck-handoff` — the only verb archive-stamp-cli
        # itself accepts, so a brief run verbatim by hand works. The short
        # `gate-recheck` stays dispatchable here because decision objects
        # written before that change carry it and are still replayable.
        # The returned `verb` echoes what was received rather than
        # normalizing, so the report names the argv actually dispatched.
        if len(args) != 3:
            raise UnrecognizedDirective(
                f"archive-stamp-cli {verb}: expected 2 arguments (<handoff_path> <at>)"
            )
        handoff_path = _assert_in_repo_root(Path(args[1]), repo_root)
        at = args[2]
        # Review: staff-eng — cs_gate_recheck_handoff's real refusal reason
        # (handoff.transition's MutateAbort text) only ever reached
        # sys.stderr; capture it here so it lands in report["error"] instead
        # of being lost to a JSON-consuming caller. No guessed cause: there
        # are several possible refusal reasons and naming one is a guess.
        buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                rc = cs_gate_recheck_handoff(str(handoff_path), at=at, cleared=True)
        finally:
            # Review: coordinator:code-reviewer — an unexpected exception from
            # cs_gate_recheck_handoff (anything the verb handler itself
            # doesn't catch and convert to an error dict) used to propagate
            # out of the `with` block before the captured buffer was ever
            # flushed to real stderr, silently discarding the refusal detail
            # this handler exists to surface. Flush unconditionally instead.
            sys.stderr.write(buf.getvalue())
        ok = _normalize_primitive_result(rc)
        if not ok:
            raise RuntimeError(
                f"archive-stamp-cli {verb} {args[1]}: refused — "
                f"{buf.getvalue().strip() or 'no detail on stderr'}"
            )
        return {"cli": "archive-stamp-cli", "verb": verb, "handoff_path": args[1]}

    if verb == "restamp-execution-sha":
        # AC18 (chunk C9) — re-stamps `execution_authorized_sha` on the ONE
        # `target_path` `build_execution_stamp_directive` names (repo-relative;
        # may be a plan a consuming handoff only POINTS at, not the artifact
        # `apply()` was itself invoked on — see `compute_execution_stamp_match`'s
        # docstring). Single-file scoped: this handler touches no path beyond
        # the one it is given.
        if len(args) != 3:
            raise UnrecognizedDirective(
                "archive-stamp-cli restamp-execution-sha: expected 2 arguments"
            )
        target_path = _assert_in_repo_root(Path(args[1]), repo_root)
        computed_sha = args[2]
        try:
            locked_rmw(
                target_path,
                _restamp_execution_sha_mutate(computed_sha),
                repo_root=repo_root,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"archive-stamp-cli restamp-execution-sha {args[1]}: failed ({exc})"
            ) from exc
        return {
            "cli": "archive-stamp-cli",
            "verb": "restamp-execution-sha",
            "target_path": args[1],
            "computed_sha": computed_sha,
        }

    if verb == "claim-memo-stamp":
        # C8 BUILD (2) — the memo-side write of C7 Part A's frontmatter
        # claim-stamp state machine (`status: open -> in_progress`). Mirrors
        # `claim-handoff`'s own shape verbatim (single artifact-path arg,
        # `_assert_in_repo_root`-bound, same failed/RuntimeError contract).
        if len(args) != 2:
            raise UnrecognizedDirective(
                "archive-stamp-cli claim-memo-stamp: expected 1 argument"
            )
        memo_path = _assert_in_repo_root(Path(args[1]), repo_root)
        # return_result=True (C13/DR-273) — memo.transition now commits its own
        # terminal write; the SHA it produced rides back on this directive's own
        # `detail` so `apply()`'s scoped-commit no-op can be told apart from a
        # genuine nothing-happened run. See `_op_commit_sha_from_results`.
        result = cs_claim_memo_stamp(str(memo_path), return_result=True)
        ok = _normalize_primitive_result(result["exit_code"])
        if not ok:
            # Review: code-reviewer (Finding 3) — surface result["error"] rather
            # than a generic "failed", since memo_transition's _err distinguishes
            # write-landed-but-uncommitted from an ordinary pre-write refusal.
            raise RuntimeError(
                f"archive-stamp-cli claim-memo-stamp {args[1]}: "
                f"{result.get('error', 'failed')}"
            )
        return {
            "cli": "archive-stamp-cli",
            "verb": "claim-memo-stamp",
            "memo_path": args[1],
            "commit_sha": result.get("commit_sha"),
        }

    if verb == "action-memo":
        # C8 BUILD (2)+(5) — the disposition-gated terminal write. `args[1]`
        # is the memo path; everything past it is the disposition-flag
        # surface `_build_action_memo_args` (`pickup_assemble/__init__.py`)
        # already resolved from the EM's content — this handler supplies no
        # command syntax of its own, only forwards `cs_action_memo`'s own
        # `*disposition_args` contract.
        if len(args) < 2:
            raise UnrecognizedDirective("archive-stamp-cli action-memo: expected >= 1 argument")
        memo_path = _assert_in_repo_root(Path(args[1]), repo_root)
        disposition_args = tuple(args[2:])
        # return_result=True (C13/DR-273) — see the claim-memo-stamp handler's
        # own comment above; same additive commit_sha capture.
        result = cs_action_memo(str(memo_path), *disposition_args, return_result=True)
        ok = _normalize_primitive_result(result["exit_code"])
        if not ok:
            # Review: code-reviewer (Finding 3) — surface result["error"] rather
            # than a generic "failed", same reasoning as claim-memo-stamp above.
            raise RuntimeError(
                f"archive-stamp-cli action-memo {args[1]}: "
                f"{result.get('error', 'failed')}"
            )
        return {
            "cli": "archive-stamp-cli",
            "verb": "action-memo",
            "memo_path": args[1],
            "disposition_args": list(disposition_args),
            "commit_sha": result.get("commit_sha"),
        }

    raise UnrecognizedDirective(f"archive-stamp-cli: unrecognized verb {verb!r}")


def _dispatch_coordinator_tasks_mirror(args: list[str], repo_root: Path) -> dict[str, Any]:
    if not args or args[0] != "init":
        raise UnrecognizedDirective(f"coordinator-tasks-mirror: unrecognized verb {args[:1]!r}")
    if len(args) != 3:
        raise UnrecognizedDirective("coordinator-tasks-mirror init: expected 2 arguments")
    basename, assertion = args[1], args[2]
    basename = _reject_path_traversal(basename, label="coordinator-tasks-mirror basename")
    session_id = _session_id_in_scope()
    if not session_id:
        raise NoResolvableSessionId("coordinator-tasks-mirror init: no session id in scope")
    module = _load_tasks_mirror_module()
    _, mirror_file = module._mirror_paths(str(repo_root), session_id, basename)
    _assert_in_repo_root(Path(mirror_file), repo_root)
    ok = _normalize_primitive_result(module.cmd_init(str(repo_root), session_id, basename, [assertion]))
    if not ok:
        raise RuntimeError(f"coordinator-tasks-mirror init {basename}: failed")
    return {"cli": "coordinator-tasks-mirror", "verb": "init", "basename": basename}


#: C4 discriminator decision (docs/plans/2026-08-19-directives-name-an-op-not-
#: a-cli.md § C4/§ The discriminator for the mixed end state) — checked live
#: against `coordinator_core.ipc._REGISTRY` this chunk: NONE of the three
#: verbs below (`session-claim-cli`, `archive-stamp-cli`,
#: `coordinator-tasks-mirror`) resolve to a registered op, so ALL THREE stay
#: `cli`-named — none migrate to `op`. No new op is minted to force a
#: migration (out of scope by name). None is a genuinely external-program
#: spawn resolved directly off the directive (`archive-stamp-cli`'s composed
#: primitives reach `git` several layers down, inside `archive_stamp.py`'s
#: own `_run_git`, never off `directives[].args` — see this module's own
#: docstring negative-spec) and none is a `CONSUMES_MANIFEST`-driven script
#: module in the completion-family sense (`coordinator-tasks-mirror` loads
#: one fixed, hand-pinned script path, not a manifest-enumerated set) — so
#: neither `docs/reference/shell-out-carve-outs.md` nor a `CONSUMES_MANIFEST`
#: entry applies to any of the three. Consequently `ASSEMBLER_DISPATCHABLE`
#: gains NO `"pickup_assemble"` entry from this chunk (C1's "ship it EMPTY
#: except for entries actually migrated" — zero migrated here).
#:
#: THE closed dispatch table (AC9e). Every key is a literal string written
#: here by hand — this dict is never mutated at runtime and never consulted
#: via anything but a plain `dict.get`/`in` on a `directives[].cli` value.
_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {
    "session-claim-cli": _dispatch_session_claim_cli,
    "archive-stamp-cli": _dispatch_archive_stamp_cli,
    "coordinator-tasks-mirror": _dispatch_coordinator_tasks_mirror,
}


def _resolve_cli(cli_name: str) -> Callable[[list[str], Path], dict[str, Any]]:
    """The one seam `directives[].cli` ever passes through. Closed over a
    literal dict (AC9e red test (a)) — an unrecognized name raises before any
    directive in the run has executed. Thin wrapper binding `apply_base.
    resolve_cli`'s generic (dispatch_table, cli_name) signature to this
    module's own `_CLI_DISPATCH`."""
    return apply_base.resolve_cli(_CLI_DISPATCH, cli_name)


# ---------------------------------------------------------------------------
# Session-scoped decision-object file (the Director of Engineering review, F6) — the hold-path
# discharge. The auto-fire hook (C3) writes the full recomputed decision
# object here after a compute-only run halts at a judgment point; the EM
# fills in each open `judgment_points[]` entry's `disposition` slot in that
# SAME file, then re-invokes `apply`/`drop`, which reads the dispositions
# back out rather than requiring a `--decisions` JSON blob be retyped.
# ---------------------------------------------------------------------------

def _session_decision_file_dir(root: Path) -> Path:
    return root / ".git" / "coordinator-sessions" / "decisions"


def _sanitize_for_filename(value: str) -> str:
    """Collapses path separators to `__` so an artifact path or session id
    round-trips into one flat, collision-resistant filename component — never
    used to resolve a filesystem path back OUT of this directory, so no
    traversal bound applies here (contrast `_reject_path_traversal`, which
    guards a value a composed primitive DOES use to build a path)."""
    return value.replace("/", "__").replace("\\", "__")


def _session_decision_file_path(root: Path, session_id: str, artifact_path: str) -> Path:
    """The ONE deterministic location both the auto-fire hook and `apply`
    independently compute from the same two inputs (session id, artifact
    path) — no value is ever passed hook-to-apply out of band."""
    name = f"{_sanitize_for_filename(session_id)}__{_sanitize_for_filename(artifact_path)}.json"
    return _session_decision_file_dir(root) / name


#: C8 BUILD (5)(i) — the EM-content keys `_read_session_dispositions` retains
#: alongside `disposition`, on top of whichever slot the EM filled on a
#: `judgment_points[]` entry. This is the memo action-memo content channel
#: (`_build_action_memo_args`, `pickup_assemble/__init__.py`) — without these,
#: an action-memo directive would be built with disposition but no content
#: and fail `cs_action_memo`'s own precondition the instant it dispatches.
#: Sourced from `pickup_assemble.DISPOSITION_CONTENT_KEYS` — the SAME set
#: `validate_decisions_shape` accepts on the `--decisions` CLI payload, so a
#: field an operator can pass in is exactly the field this reader retains;
#: no second, drifting copy of the key list.
_DISPOSITION_CONTENT_KEYS = DISPOSITION_CONTENT_KEYS


def _read_session_dispositions(root: Path, session_id: str, artifact_path: str) -> dict[str, Any]:
    """Reads the session-scoped decision-object file (if present) and
    extracts a `--decisions`-shaped accumulation map
    (`{judgment_point_id: {disposition: str, ...}}`) from any
    `judgment_points[]` entry the EM has filled a non-null `disposition` slot
    on. Alongside `disposition`, retains whichever of
    `_DISPOSITION_CONTENT_KEYS` the EM also wrote onto that same entry — the
    memo action-memo directive's content channel (`_build_action_memo_args`)
    reads `decision_note`/`realized_by`/`actioned_note`/`distill_fate` off
    exactly this map; dropping them here would silently truncate the
    directive's args to disposition-only and starve it of content.

    Absent file, unreadable file, malformed JSON, or a payload missing/
    misshaping `judgment_points` all degrade to `{}` — never raise. A
    dispositions map derived here is strictly additive input to `brief()`'s
    own recompute; failing to read it must never be worse than not having it
    (the coast stays exactly as blocked as it already is).
    """
    path = _session_decision_file_path(root, session_id, artifact_path)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    judgment_points = payload.get("judgment_points")
    if not isinstance(judgment_points, list):
        return {}

    dispositions: dict[str, Any] = {}
    for jp in judgment_points:
        if not isinstance(jp, dict):
            continue
        jp_id = jp.get("id")
        disposition = jp.get("disposition")
        if jp_id and disposition:
            entry: dict[str, Any] = {"disposition": disposition}
            for key in _DISPOSITION_CONTENT_KEYS:
                value = jp.get(key)
                if value:
                    entry[key] = value
            dispositions[jp_id] = entry
    return dispositions


# ---------------------------------------------------------------------------
# Session-id propagation (AC9(a)) — explicit only, never the ambient tier-4
# sentinel.
# ---------------------------------------------------------------------------

def _resolve_explicit_session_id(session_id: Optional[str]) -> Optional[str]:
    return apply_base.resolve_explicit_session_id(session_id, env_read_order=_SESSION_ENV_READ_ORDER)


def _session_identity(session_id: str):
    """Thin wrapper binding `apply_base.session_identity`'s generic
    `env_vars` parameter to this module's own `_SESSION_ENV_VARS` pair —
    `apply_base.session_identity` is itself a `@contextmanager`, so this
    plain function returning its context manager is `with`-usable
    unchanged."""
    return apply_base.session_identity(session_id, env_vars=_SESSION_ENV_VARS)


def _session_id_in_scope() -> Optional[str]:
    """The id an IN-PROCESS directive handler must resolve identity from.

    NEGATIVE SPEC — do not reduce this to an `os.environ` read. Since chunk
    C6, `apply_base.session_identity` scopes the id into a `contextvars`
    ContextVar and NEVER into `os.environ`, precisely so two overlapping
    warm dispatches cannot overwrite each other's ambient identity. A
    handler that reads `os.environ` directly therefore no longer sees the
    id `_apply` resolved and entered the scope with — it sees whatever the
    launch environment happened to carry, which is either nothing (raising
    `NoResolvableSessionId` on a session that has a perfectly good id) or,
    worse, a DIFFERENT session's id read as this one's.

    The `os.environ` fallback is retained only for a caller that never
    entered `_session_identity()` at all, where the launch environment IS
    the correct source.

    Spec backlink: `docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md`
    chunk C6.
    """
    scoped = apply_base.current_session_env(env_vars=_SESSION_ENV_VARS)
    for var in _SESSION_ENV_VARS:
        val = (scoped.get(var) or "").strip()
        if val:
            return val
    for var in _SESSION_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None


class _UnresolvableArtifactClass(Exception):
    """Raised by `_class_and_basename` when `artifact["classification"]` is
    none of the shapes it knows how to fold into a `memo`/`handoff` claim
    class — never silently guessed. See that function's own docstring for
    the incident this replaces (2026-07-27 defect fix)."""


def _class_and_basename(artifact: dict[str, Any]) -> tuple[str, str]:
    """`artifact["classification"]` -> `(claim_class, basename)`, where
    `claim_class` is always exactly `"memo"` or `"handoff"` — the only two
    values `session-claim-cli`/`cs_unclaim_handoff`/`cs_release_memo_
    revert` know how to act on. `spinoff` folds into `handoff` (a spinoff
    IS a handoff subtype and uses the same claim/release mechanics
    throughout this module — there is no separate spinoff branch anywhere
    else in `apply.py`, so this must not introduce one).

    `archived` (`resolve_artifact`'s terminal record for a swept baton)
    does NOT itself say memo-or-handoff — that distinction lives one level
    down, at `artifact["resolution"]["archived_class"]`
    (`_build_archived_resolution`'s own `_has_memo_shape` verdict,
    preserved rather than discarded — 2026-07-27 defect fix). Before this
    fix, this function silently defaulted every non-`"memo"` classification
    to `"handoff"`, so `drop` on an ARCHIVED MEMO called
    `cs_unclaim_handoff` (a handoff-only primitive that then failed its
    own containment guard, since the path is under `cross-repo/archive/`,
    not `state/handoffs/`) instead of `cs_release_memo_revert` — confirmed
    live: `pickup-assemble drop` on an archived memo whose frontmatter
    `status:` was NOT `open`/`actioned` (so `classify()` itself returned
    `"ambiguous"` before the archive-dir override stamped it `"archived"`)
    reproduced `cs_unconsume_handoff: unconsume: handoff_path escapes
    state/handoffs/`. A classifier that guesses is the deeper defect: any
    OTHER `classification` value this function does not recognise now
    raises `_UnresolvableArtifactClass` rather than picking one — both
    `apply()` and `drop()` catch it and return `APPLY_EXIT_TRANSPORT_FAIL`
    (an honest "could not resolve a class" instead of a wrong guess or an
    uncaught crash).
    """
    classification = artifact.get("classification")
    basename = Path(artifact.get("path", "")).name
    if classification in ("handoff", "spinoff"):
        return "handoff", basename
    if classification == "memo":
        return "memo", basename
    if classification == "archived":
        archived_class = (artifact.get("resolution") or {}).get("archived_class")
        if archived_class in ("memo", "handoff"):
            return archived_class, basename
    raise _UnresolvableArtifactClass(
        f"cannot determine memo/handoff class for {artifact.get('path')!r} "
        f"(classification={classification!r}) — refusing to guess"
    )


def _execute_directives(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    repo_root: Path,
    *,
    decisions: Optional[dict[str, Any]] = None,
    resolve_claim_grant: Optional[Callable[[], dict[str, Any]]] = None,
    composition_budget: "Optional[apply_base.CompositionBudget]" = None,
) -> tuple[int, dict[str, Any]]:
    """THE directive-execution seam (AC5c/AC5d unit-test seam decided C1d,
    shared with C2a's AC9e(a) test) — callable directly with two
    `judgment_points` lists differing only in `recommendation` content to
    prove the scoped predicate: the executed-directive log and resulting
    on-disk state are identical whether or not a `recommendation` is present.

    AMENDMENT 2026-07-24 (chunk C7 Part B) — the halt is now PER-DIRECTIVE,
    not a blunt "any non-empty `judgment_points` halts everything before any
    directive executes" rule. A directive with `depends_on: None`, or whose
    every named judgment-point dependency is resolved to a disposition whose
    OWN `resolves` list names this directive (`_disposition_resolves_
    directive` — the Director of Engineering v2 finding-1 value-aware predicate, NOT a plain
    "has a disposition been set" check), fires. A directive whose `depends_on`
    names an unresolved (or non-terminally-resolved) judgment point does not
    — the run still reports `APPLY_EXIT_HALTED_AT_JUDGMENT` overall whenever
    at least one directive was blocked this way, but every OTHER directive
    that reached "ready" this pass still dispatches (e.g. a memo's
    mechanical claim-grab fires under a still-open kind-dispatch judgment
    point). The `recommendation` key on any entry is never read here (AC5c:
    nothing auto-resolves; AC5d: `recommendation` is never a control-flow
    input) — only `decisions[jp_id].disposition` and the CHOSEN disposition's
    own `resolves` list are consulted.

    Directives with zero directives to consider at all (`directives == []`)
    still fall back to the old blunt behaviour: a non-empty `judgment_points`
    with nothing to dispatch is unconditionally `APPLY_EXIT_HALTED_AT_
    JUDGMENT` (e.g. the live-claim stand-down offer, which returns
    `directives: []` alongside `judgment_points: [j1]`) — there is no
    directive for the per-directive predicate to differentiate.

    `claim_grant` (via `resolve_claim_grant`, when supplied) is re-resolved
    immediately before any directive dispatches, UNCONDITIONALLY on
    `judgment_points`' contents — the pre-loop blanket DENIED gate is
    untouched by this amendment (Part B safety constraint (b); the Director of Engineering v2
    finding 6): mirror-init and every other `depends_on: None` directive
    only ever auto-fires once this gate has already cleared, so open-
    liveness auto-fire cannot race a real peer holder, by construction of
    this gate running first.

    Otherwise orders execution-ready directives by `depends_on` (directive-
    to-directive ordering; a judgment-point id in `depends_on` is not a
    member of the directive-id set and is therefore ignored by this
    ordering step — see `apply_base.order_by_depends_on`), skips `already_satisfied`
    directives without dispatching their handler, and reports one
    `DirectiveResult` per directive that actually dispatched (or was skipped
    as `already_satisfied`) in `report["results"]` / `report["landed"]` — a
    directive blocked by an unresolved judgment point never appears in
    either (AC9g's partial-mutation contract: only directives that actually
    reached "landed" status are eligible for `report["landed"]`, and by
    extension for `apply()`'s scoped commit).

    Thin wrapper binding `apply_base.execute_directives`'s generic
    `dispatch_table` parameter to this module's own closed `_CLI_DISPATCH` —
    every other parameter and the returned `(exit_code, report)` shape pass
    through unchanged. `composition_budget` is threaded in from `apply()`'s
    own construction, never built here, so its lifetime is the apply run's
    and not this wrapper's.
    """
    return apply_base.execute_directives(
        directives,
        judgment_points,
        repo_root,
        _CLI_DISPATCH,
        decisions=decisions,
        resolve_claim_grant=resolve_claim_grant,
        composition_budget=composition_budget,
    )


# ---------------------------------------------------------------------------
# Scoped commit (AC10) — the ONE commit apply ever makes, pathspec-limited to
# the artifact `apply` itself just mutated. `apply` is invoked in a shared
# concurrent-EM working tree where a sibling session's own edits may already
# sit staged in the same index — `git add -A`/`git commit` with no pathspec
# would sweep those peer files into this run's commit. Every git call below
# instead names the one resolved artifact path explicitly, both on `add` and
# on `commit`: `git commit -- <path>` composes a partial commit of exactly
# that path's staged+worktree state regardless of what else is staged,
# leaving any pre-existing staged peer files exactly as they were.
# ---------------------------------------------------------------------------

def _compute_commit_message(class_: str, basename: str, landed: list[str]) -> str:
    """Composes apply's own commit message — never author-supplied, never a
    brief-derived string reused verbatim. `landed` names the directive ids
    that actually dispatched (or were skipped as `already_satisfied`) on this
    run, in execution order."""
    summary = ", ".join(landed) if landed else "no-op"
    return f"pickup-assemble apply: {class_} {basename} ({summary})"


def _scoped_commit(
    repo_root: Path,
    artifact_rel_path: str,
    class_: str,
    basename: str,
    landed: list[str],
) -> Optional[str]:
    """Stages then commits ONLY `artifact_rel_path`, via an explicit pathspec
    on both the `add` and the `commit` (AC10). Returns the new commit's SHA,
    or `None` when there was nothing to commit for this path (a clean run
    whose directives were all `already_satisfied`, or an artifact path this
    run never actually wrote to) — a no-op, not a failure.

    Never resolves `_run_git`'s `cwd` from anything but the caller-supplied
    `repo_root`, and never widens the pathspec beyond the one resolved path —
    there is no seam here through which a second path could be added to this
    commit.

    Thin wrapper binding `apply_base.scoped_commit`'s generic `message`/
    `run_git` parameters to this module's own `_compute_commit_message`
    shape and `_run_git` in-process read-model.
    """
    message = _compute_commit_message(class_, basename, landed)
    return apply_base.scoped_commit(repo_root, artifact_rel_path, message, _run_git)


def _op_commit_sha_from_results(report: dict[str, Any]) -> Optional[str]:
    """Recovers the terminal write's own commit SHA when `_scoped_commit`'s
    pathspec-diff found nothing staged to commit for THIS run (C13/DR-273).

    `memo.transition` now takes commit ownership of its own terminal write
    (`claim`/`action`/`release`/`resolve`) — a successful `claim-memo-stamp`
    or `action-memo` directive dispatched by this run has ALREADY committed
    the memo path by the time `_scoped_commit` runs its own `git add` +
    `git diff --cached --quiet` check, so that check legitimately finds
    nothing dirty and `_scoped_commit` returns its own no-op `None`.
    `_scoped_commit`'s `None` therefore no longer distinguishes "the op
    already committed this path" from "genuinely nothing happened" — this
    function is what tells the two apart, by reading the SHA the dispatched
    directive itself reported (via `_dispatch_archive_stamp_cli`'s
    `claim-memo-stamp`/`action-memo` handlers, `commit_sha` additive key on
    `report["results"][*]["detail"]`).

    Returns the LAST non-None `commit_sha` among `report["results"]`, in
    landed (execution) order — the most recent terminal write this run
    actually made. Returns `None` when no directive result carries one,
    which is exactly the genuinely-nothing-happened case: a run with no
    committing directive dispatched has no `commit_sha` on any result, so
    this degrades to `_scoped_commit`'s own correct `None` for that case —
    the two callers of `_scoped_commit` (`apply`, `drop`) never need to
    track "did the op commit" as separate state of their own.
    """
    sha: Optional[str] = None
    for result in report.get("results", []) or []:
        detail = result.get("detail") or {}
        if not isinstance(detail, dict):
            continue
        candidate = detail.get("commit_sha")
        if candidate:
            sha = candidate
    return sha


def apply(
    artifact_path: str,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
    decisions: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any]]:
    """`apply <artifact-path> [--session-id <id>] [--decisions <json>]` —
    recomputes the brief in-process and executes its `directives[]` through
    the closed dispatch table. Returns `(exit_code, report)`;
    `report["landed"]` names exactly which directive ids mutated state, per
    AC9g's partial-mutation contract.

    Dispositions (the Director of Engineering review, F6 — the hold-path residue): when `decisions`
    is `None` (the EM's primary path — no flag to recall), `apply` reads the
    session-scoped decision-object file the auto-fire hook wrote and derives
    its dispositions map from there (`_read_session_dispositions`). Passing
    `decisions` explicitly (the `--decisions` CLI flag) is the crash-resume/
    audit surface — an explicit value always wins over the file, never merged
    with it.

    A clean run (`APPLY_EXIT_OK`) composes the scoped commit (AC10) on top:
    the mutated artifact is staged and committed via an explicit pathspec,
    never touching any other path a sibling session may already have staged
    in the same shared index. `report["commit_sha"]` is the new commit's SHA,
    or `None` when the run landed nothing to commit (e.g. every directive was
    `already_satisfied`). Any OTHER exit code (halted-at-judgment,
    claim-denied, transport-failure, partial-mutation) commits nothing.
    """
    root = repo_root or _resolve_repo_root_for_apply()
    if root is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": "could not resolve a git worktree root"}

    composition_budget = make_fleet_budget("pickup_assemble")

    resolved_sid = _resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": (
                "no session id resolvable via --session-id or "
                f"{'/'.join(_SESSION_ENV_READ_ORDER)} — refusing the ambient "
                "tier-4 sentinel (AC9(a))"
            ),
        }

    # PM ruling D-G (chunk C9): a pickup EM from a different repo than the
    # artifact must be DENIED, by name, not merely fail to resolve a path.
    # Hoisted to BEFORE `brief()`/`resolve_artifact` (2026-08-30): once
    # `resolve_artifact` normalizes an absolute path itself, it raises
    # `OutOfRepoPath` first and this named denial would become unreachable,
    # degrading into a generic transport-fail — checked here, on the PASSED
    # `artifact_path`, so the named reason still surfaces. Every fallback
    # tier inside `resolve_artifact` is repo_root-anchored, so a relative
    # input can never resolve outside `root` anyway; only an absolute (or
    # `..`-traversal) input can trip this, same as the resolved-path check
    # it replaces. A cross-repo memo sitting in THIS repo's own
    # `cross-repo/inbox/` resolves UNDER `root` (it is this repo's own
    # artifact) and is unaffected — the denial keys on containment under
    # `root`, never on the string "cross-repo" appearing in a path.
    if artifact_path:
        try:
            _assert_in_repo_root(Path(artifact_path), root)
        except OutOfRepoPath:
            return APPLY_EXIT_CLAIM_DENIED, {
                "reason": "cross_repo_pickup_denied",
                "error": (
                    f"{artifact_path}: not under this session's repo root "
                    f"({root}) — pickup denied"
                ),
                "landed": [],
            }

    with _session_identity(resolved_sid):
        effective_decisions = (
            decisions if decisions is not None else _read_session_dispositions(root, resolved_sid, artifact_path)
        )
        try:
            # Deliberately WITHOUT `claim_at_brief`: `apply` has no unguarded
            # window to protect. Its own `d1` (`claim-artifact`) already takes
            # the lock at `apply` stage for an `apply` run with no prior
            # `brief`, and for the ordinary brief-then-apply path the lock is
            # already self-held and only needs promoting (below).
            brief_result = brief(artifact_path, decisions=effective_decisions, repo_root=root)
        except Exception as exc:  # noqa: BLE001 - mirrors brief()'s own main() backstop
            return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)}

        decision = brief_result.decision_object
        directives = decision.get("directives", [])
        judgment_points = decision.get("judgment_points", [])
        artifact = decision.get("artifact", {})

        # judgment_points takes priority over every other check below — an
        # artifact whose brief() computation bailed out early with a
        # non-empty judgment_points (e.g. the live-claim-holder stand-down
        # offer, which returns EXIT_BUSINESS_FAIL with directives=[]) still
        # halts-at-judgment (AC5c), never transport-fails.
        if not judgment_points and brief_result.exit_code != _BRIEF_EXIT_OK and not directives:
            # brief()'s live-claim-holder stand-down (both branches — see
            # `pickup_assemble.__init__.py`'s memo and handoff stand-down
            # blocks, cross-repo/inbox/2026-08-17-doe-claude-em-memo-claim-
            # fires-after-the-em-can-already-act.md, commit `3c779cde2`)
            # lands exactly HERE whenever no durable frontmatter claim
            # stamp exists yet — the normal case for a peer holding only
            # the brief-stage side-file lock, where `gates.liveness_signal`
            # never fires and `judgment_points` comes back empty alongside
            # `directives: []`. That is a genuine, correctly-detected claim
            # contention, not "brief did not resolve a plan" — classify off
            # `gates.claim_grant` (the SAME field `apply()`'s own pre-loop
            # blanket gate below reads) before falling back to the honest
            # transport-failure bucket, so a real contention reports
            # `APPLY_EXIT_CLAIM_DENIED`, never an infrastructure fault a
            # wrapper/hook would branch on wrong.
            claim_grant = (decision.get("gates") or {}).get("claim_grant")
            if isinstance(claim_grant, dict) and claim_grant.get("verdict") not in apply_base.GRANTED_VERDICTS:
                return APPLY_EXIT_CLAIM_DENIED, {
                    "claim_grant": claim_grant,
                    "landed": [],
                }
            # Every other early bail is a genuine "brief itself did not
            # resolve to a plan" (ambiguous artifact, multi-hit archive
            # fallback, memo addressee block) — nothing was ever computed
            # to mutate; not a crash, but nothing apply can proceed on
            # either. The honest bucket for that case, distinct from the
            # denied-claim arm just above and from an unresolved judgment
            # point (handled by the priority check this comment sits under).
            return APPLY_EXIT_TRANSPORT_FAIL, {
                "error": decision.get("error", "brief did not resolve an actionable plan"),
                "landed": [],
            }

        try:
            class_, basename = _class_and_basename(artifact)
        except _UnresolvableArtifactClass as exc:
            return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc), "landed": []}
        artifact_path_value = artifact.get("path", "")

        # 2026-07-26 defect fix (silent-discard on an already-terminal
        # artifact): `brief()` legitimately returns `directives: []` AND
        # `judgment_points: []` together for a genuinely terminal artifact
        # (an already-archived record, or the memo M0 `status: actioned`
        # short-circuit) — `apply_base.execute_directives` then reports
        # `APPLY_EXIT_OK`/`landed: []` (its own "nothing to consider" no-op
        # contract). That contract is correct when the caller supplied NO
        # dispositions. It silently discards caller intent the instant
        # `effective_decisions` is non-empty — there is no directive and no
        # judgment point left in this run for those fields to attach to, so
        # a caller-supplied `--decisions` payload (or a session-file
        # disposition) vanishes without a trace while `apply` still reports
        # success. Fail loud instead of accepting-and-discarding (EM
        # ruling, choice (b) — see this module's own docstring negative-
        # spec on `judgment_points`/`recommendation` handling for the same
        # "never silently swallow caller intent" posture): the caller must
        # see that nothing was recorded, not learn it three sessions later
        # from a memo that never got its `actioned_note`.
        if effective_decisions and not directives and not judgment_points:
            return APPLY_EXIT_TRANSPORT_FAIL, {
                "error": (
                    f"{artifact_path_value or artifact_path}: resolved to a terminal "
                    f"artifact (classification={class_!r}) with no directive or "
                    "judgment point left to apply the supplied decisions to — "
                    "nothing was recorded; decisions supplied for "
                    f"{sorted(effective_decisions.keys())}"
                ),
                "landed": [],
            }

        def _resolve_claim_grant() -> dict[str, Any]:
            # Re-resolved on every call, never cached — this is what makes
            # AC9f's general revalidate_at_dispatch rule hold for
            # `claim_grant` (and, by the same never-cache-`brief()`-output
            # construction, for every OTHER revalidating judgment point):
            # apply() never trusts a value computed earlier than the instant
            # right before it gates a mutation.
            #
            # Review: code-reviewer — Finding 3: `fm` is threaded through from
            # `brief_result.decision_object["artifact"]["frontmatter"]` to
            # match `brief()`'s handoff-branch call (`compute_claim_grant(...,
            # fm=fm)`). Without it, AC3e's lineage-handover row degrades to
            # pre-AC3e "always denied for any live non-self holder", so a
            # lineage-related live holder `brief()` correctly classified as a
            # handover could be re-classified `denied` at dispatch time even
            # though the lineage relationship never changed between calls —
            # an asymmetry, not a deliberate stricter dispatch-time check.
            return compute_claim_grant(
                root, class_, basename, artifact_path_value, cwd=str(root), fm=artifact.get("frontmatter")
            )

        # Promote the pre-work reservation into a durable claim before any
        # directive runs. Explicit rather than a side effect of d1: once
        # `brief` takes the lock, `_claim_already_self_held` correctly marks
        # d1 `already_satisfied`, so the `claim_artifact` call that would
        # otherwise have written the stage never happens — and a claim left at
        # `brief` stage keeps a lease it has outgrown, becoming takeable out
        # from under a session that is mid-mutation. A no-op for a claim
        # already at `apply` stage, and for one held by anybody else.
        promote_claim_stage(class_, basename, cwd=str(root))

        outcome = "directive_failed"
        try:
            exit_code, report = _execute_directives(
                directives,
                judgment_points,
                root,
                decisions=effective_decisions,
                resolve_claim_grant=_resolve_claim_grant,
                composition_budget=composition_budget,
            )
            if exit_code == APPLY_EXIT_OK:
                outcome = "success"
            elif exit_code == APPLY_EXIT_PARTIAL_MUTATION:
                outcome = "partial_mutation"
        finally:
            flush_composition_record(composition_budget, outcome)

        # AMENDMENT 2026-07-24 (chunk C7 Part B(c), the Director of Engineering v2 finding 3, EM
        # ruling (a) BANK-THE-GRAB) — the "only APPLY_EXIT_OK commits"
        # invariant is widened to also commit on APPLY_EXIT_HALTED_AT_
        # JUDGMENT: a per-directive mixed-halt run may still have banked a
        # real mechanical mutation (e.g. C8's claim-memo-stamp landing while
        # the kind-dispatch judgment point stays open) that must not sit
        # uncommitted in a shared worktree across the EM's decision
        # round-trip. `_scoped_commit` already no-ops (returns `None`) when
        # nothing actually changed on disk for this artifact path — on the
        # handoff path specifically, a mixed halt typically has nothing new
        # to commit yet (its one artifact-mutating directive, `d2`, is
        # exactly what stayed gated), so this widening does not fabricate a
        # commit where the halted run landed nothing.
        if exit_code in (APPLY_EXIT_OK, APPLY_EXIT_HALTED_AT_JUDGMENT):
            scoped_sha = _scoped_commit(
                root, artifact_path_value, class_, basename, report.get("landed", [])
            )
            # C13/DR-273 — a `None` here is ambiguous between "nothing landed"
            # and "the dispatched op (memo.transition) already committed this
            # exact path itself". `_op_commit_sha_from_results` resolves the
            # ambiguity by reading the SHA a committing directive's own
            # `detail` reported, and correctly falls through to `None` again
            # when nothing did.
            report["commit_sha"] = scoped_sha if scoped_sha is not None else _op_commit_sha_from_results(report)

        # Hand the lease back when this run stamped NOTHING (state/bug-backlog/
        # 2026-08-26-a-halted-pickup-leaves-a-live-ledger-claim-on-a-peers-
        # baton.yaml). The promotion above fires before any directive runs, so
        # a run that halts at a judgment point — the ordinary outcome when the
        # EM has a decision to make — otherwise ends holding a lease-free
        # claim on a baton it never touched, for the rest of the session's
        # life. `/workstream-complete`'s live-consume leg reads that claim
        # ledger-first and resolves `predecessor-consumed` against it; on
        # somebody else's baton that caps a PEER's workstream.
        #
        # `claim_stamped` alone is NOT the gate (2026-08-26 code review,
        # coordinator:code-reviewer P2): `mark_claim_stamped` has exactly one
        # writer, `_dispatch_archive_stamp_cli`'s `claim-handoff` verb, but the
        # same dispatch table has two other handoff-facing verbs —
        # `gate-recheck-handoff`/`gate-recheck` and `restamp-execution-sha` —
        # that mutate real state and can land (and, under BANK-THE-GRAB above,
        # get `_scoped_commit`'d) on an `APPLY_EXIT_HALTED_AT_JUDGMENT` run
        # that halts at a LATER judgment point before `claim-handoff` (d2)
        # ever executes. `claim_stamped` reads `False` for that claim dir no
        # matter how real the banked mutation was, since no writer ever
        # touched the marker for those two verbs. Demoting on `claim_stamped`
        # alone would therefore reintroduce exactly the "claim taken out from
        # under a mid-mutation session" hazard the unconditional promotion
        # above exists to prevent. The gate is widened to also require that
        # no directive OTHER than "d1" (`build_handoff_directives`'s
        # mechanical `claim-artifact` reservation grab, ALWAYS present and
        # ALWAYS landed once a claim exists — it is exactly the reservation
        # `promote_claim_stage`/`demote_claim_stage_to_brief` already govern,
        # never itself an external-state mutation) appear in
        # `report["landed"]`. `report["landed"]` names every directive that
        # DISPATCHED without raising, not only ones that mutated durable
        # state on THIS artifact — "d1" lands on every claimed handoff apply
        # regardless of outcome (confirmed against
        # `test_apply_hands_the_lease_back_when_no_stamp_landed`, whose
        # refused `claim-handoff` still leaves `report["landed"] == ["d1"]`),
        # so treating any non-empty `landed` as "banked something" would
        # never demote a handoff run again. A run that banked ANYTHING BEYOND
        # the reservation grab — via `claim-handoff`'s marker OR any other
        # verb's landed directive — keeps its promotion; only a run that
        # banked nothing beyond the reservation AND left no marker gets
        # demoted.
        #
        # HANDOFF ONLY, and deliberately: `mark_claim_stamped` has exactly one
        # writer, `_dispatch_archive_stamp_cli`'s `claim-handoff` verb. A memo
        # claim never carries the marker no matter how cleanly its apply ran,
        # so reading `claim_stamped` there would demote every successful memo
        # pickup. Memos reach their own terminal state through
        # `memo.transition` and are out of scope for this rule until they have
        # an equivalent landed-fact of their own.
        #
        # Demote, never release: the reservation and its `brief`-stage lease
        # survive, so the EM answers the judgment point and re-runs `apply`,
        # which re-promotes. Best-effort by construction — the return value is
        # deliberately unread, exactly as `mark_claim_stamped`'s is, since a
        # ledger-hygiene write must never change what an otherwise-complete
        # apply run reports.
        _banked_beyond_reservation = [
            d for d in (report.get("landed") or []) if d != "d1"
        ]
        if class_ == "handoff" and not _banked_beyond_reservation:
            try:
                _common_dir = git_common_dir(root)
            except Exception:
                _common_dir = None
            if _common_dir is not None and not claim_stamped(
                # `basename`, not the artifact path — it is the exact claim key
                # `demote_claim_stage_to_brief` resolves below, so the read and
                # the write cannot address different dirs.
                handoff_claim_dir(_common_dir, Path(basename))
            ):
                demote_claim_stage_to_brief(class_, basename, cwd=str(root))

        return exit_code, report


def _resolve_repo_root_for_apply(start: Optional[Path] = None) -> Optional[Path]:
    from coordinator_core.pickup_assemble import resolve_repo_root as _brief_resolve_repo_root

    return _brief_resolve_repo_root(start)


def _usage(prog: str) -> int:
    print(
        f"usage: {prog} brief <artifact-path> [--decisions <json> | --decisions-file <path>]",
        file=sys.stderr,
    )
    print(
        f"       {prog} apply <artifact-path> [--session-id <id>] "
        "[--decisions <json> | --decisions-file <path>]",
        file=sys.stderr,
    )
    print(f"       {prog} drop <artifact-path> [--session-id <id>]", file=sys.stderr)
    print(
        '       --decisions is a JSON object: {"<jp-id>": {"disposition": "<value>", ...}}',
        file=sys.stderr,
    )
    print(
        '       ("value" is accepted as an exact equivalent of "disposition" -- brief\'s own',
        file=sys.stderr,
    )
    print(
        "        output uses that key). Legal <value>s for a given jp-id are that judgment",
        file=sys.stderr,
    )
    print(
        "        point's own dispositions[].value entries from this run's `brief` output.",
        file=sys.stderr,
    )
    return APPLY_EXIT_TRANSPORT_FAIL


def _reject_if_multi_artifact(artifact_path: str, subcmd: str, prog: str) -> Optional[int]:
    """Fails loud, rather than silently mutating the WRONG artifact, when
    `artifact_path` names more than one baton (an ` AND `-joined argument, a
    `{...}` brace group, or a pasted `- `/`* ` bullet list — anything
    `split_artifact_args` would fan out).

    2026-08-11 defect (same harm class as the `brief`-side fix this mirrors):
    unlike `brief`, neither `apply` nor `drop` ever called
    `split_artifact_args` — a multi-artifact argument passed straight into
    `resolve_artifact` as ONE literal path, where `_sanitize_artifact_path_
    str`'s hard-line-wrap rejoin could mash a bulleted two-path paste into a
    single string that happened to suffix-match ONE of the two artifacts.
    For `brief` that silently returned the wrong SURVEY entry; for `apply`/
    `drop` the same silent collapse would silently CLAIM, COMMIT, or RELEASE
    against the wrong artifact while naming none of this to the caller —
    worse than `brief`'s read-only case, since it mutates.

    `brief` itself is deliberately NOT given this same rejection: its
    array-fan-out over N independent `brief()` calls is an existing, HARD
    cross-repo consumer contract (`coordinator/hooks/scripts/pickup-
    autofire.py` parses exactly that shape) and is read-only, so fanning out
    is the correct behavior there, not the defect. `apply`/`drop` mutate and
    were never given a multi-artifact contract at all, so refusing is the
    right shape for them — one subcommand, one artifact, one object.

    Returns `None` when `artifact_path` names exactly one artifact (the
    common case — a no-op check). Returns `APPLY_EXIT_TRANSPORT_FAIL` after
    printing a loud, itemized stderr message when it names more than one —
    the caller returns this value directly rather than proceeding.
    """
    paths = split_artifact_args(artifact_path)
    if len(paths) <= 1:
        return None
    print(
        f"pickup-assemble {subcmd}: {artifact_path!r} parses as {len(paths)} artifacts, "
        f"not one — {subcmd} acts on exactly one artifact at a time:",
        file=sys.stderr,
    )
    for i, path in enumerate(paths, start=1):
        print(f"  {i}. {path}", file=sys.stderr)
    print(
        f"       Run `{prog} {subcmd} <artifact-path>` once per artifact above "
        f"(use `{prog} brief <artifact-path> AND <artifact-path> ...` first if you "
        "need a read-only survey across all of them before committing to any one).",
        file=sys.stderr,
    )
    return APPLY_EXIT_TRANSPORT_FAIL


def main_apply(argv: list[str]) -> int:
    """`main()`'s `apply` dispatch arm — parses argv, calls `apply()`, prints
    the report, returns its exit code. `--decisions` is the crash-resume/
    audit surface (the Director of Engineering review, F6) — omitting it is the EM's primary path,
    which reads dispositions from the session-scoped decision file instead."""
    if not argv:
        return _usage("pickup-assemble")

    artifact_path = argv[0]
    reject = _reject_if_multi_artifact(artifact_path, "apply", "pickup-assemble")
    if reject is not None:
        return reject
    tail = argv[1:]
    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    conflict = detect_conflicting_payload_channels(tail)
    if conflict is not None:
        print(f"pickup-assemble apply: {conflict}", file=sys.stderr)
        return _usage("pickup-assemble")
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--session-id":
            if i + 1 >= len(tail):
                return _usage("pickup-assemble")
            session_id = tail[i + 1]
            i += 2
        elif (payload := resolve_json_payload_flag(tail, i)).consumed:
            if payload.error is not None:
                print(f"pickup-assemble apply: {payload.error}", file=sys.stderr)
                return _usage("pickup-assemble")
            decisions = payload.value
            shape_error = validate_decisions_shape(decisions)
            if shape_error is not None:
                print(f"pickup-assemble apply: {shape_error}", file=sys.stderr)
                return _usage("pickup-assemble")
            i += payload.consumed
        else:
            print(f"pickup-assemble apply: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage("pickup-assemble")

    exit_code, report = apply(artifact_path, session_id=session_id, decisions=decisions)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


# ---------------------------------------------------------------------------
# `drop` — the clean full inverse of a granted claim (§ "Dropping the baton
# is a first-class exit"). Composes `claims.release_artifact` +
# `archive_stamp.cs_unclaim_handoff` unconditionally: neither primitive is
# skipped or conditioned on which of a prior `apply` run's directives
# actually landed (AC9g) — each is idempotent by construction (a no-op
# release on a not-yet-claimed artifact; a no-op unclaim on a handoff
# already at `open`+`ready_to_fire`), so a half-applied artifact returns
# cleanly without this module inspecting or reconstructing partial-apply
# state.
# ---------------------------------------------------------------------------

def _recorded_claim_holder(claim_dir: Path) -> str:
    """Best-effort human-readable name of `claim_dir`'s recorded holder, for
    the non-holder drop refusal's `error` text ONLY — never used as a control-
    flow input (`liveness.claim_held_by_me` is the sole holder predicate).
    Session-id claim dirs report the recorded session id; legacy pid-only
    dirs report `pid:<n>`; an unreadable/unrecognized dir reports
    `"unknown"` rather than raising, since this is diagnostic text on an
    already-decided refusal."""
    sid_file = claim_dir / "session_id"
    if sid_file.is_file():
        try:
            recorded = sid_file.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = ""
        return recorded or "unknown"
    pid_file = claim_dir / "pid"
    if pid_file.is_file():
        try:
            recorded_pid = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            recorded_pid = ""
        return f"pid:{recorded_pid}" if recorded_pid else "unknown"
    return "unknown"


def drop(
    artifact_path: str,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> tuple[int, dict[str, Any]]:
    """`drop <artifact-path> [--session-id <id>]` — returns the artifact to
    `open` + `ready_to_fire` with the claim record wiped, as if the pickup
    never happened. Returns `(exit_code, report)` using the SAME exit-code
    contract `apply` pins (AC9g): `APPLY_EXIT_OK` on a clean drop,
    `APPLY_EXIT_TRANSPORT_FAIL` when the repo root/session id/artifact
    itself cannot be resolved, `APPLY_EXIT_CLAIM_DENIED` when the caller is
    not the recorded holder of a stamped (non-brief-stage) claim — the SAME
    exit code the cross-repo denial above already returns, so a non-holder
    drop is a reason rather than a silent half-mutation (see the holder gate
    below) — `APPLY_EXIT_PARTIAL_MUTATION` when the handoff/memo-transition
    primitive itself reports failure (a genuine primitive error, distinct
    from the ordinary not-yet-claimed no-op it tolerates), OR when the
    frontmatter revert and ledger release have both landed but the terminal
    `_scoped_commit` raises — the commit tail's `RuntimeError` is caught
    (only there, not the primitives above it) and reported as
    `APPLY_EXIT_PARTIAL_MUTATION` with `released: True`, `unclaimed` as the
    class inverse actually returned, and `commit_sha: None`, naming the
    underlying git error rather than letting it escape as a traceback.

    HOLDER GATE (2026-08-30): before either composed primitive runs, and
    before `_scoped_commit`, `drop` reads the claim ledger dir ONCE for a
    non-brief-stage claim and trichotomizes: (i) the ledger dir is absent —
    proceed (nothing to protect; this is the state a reaped or
    crash-truncated session leaves behind, with a live frontmatter stamp
    and no verb but this one able to clear it), (ii) `liveness.
    claim_held_by_me` says this session is the recorded holder — proceed,
    (iii) the dir is present and recorded as held by someone else — refuse
    with `APPLY_EXIT_CLAIM_DENIED`, naming the recorded holder
    (`docs/wiki/claim-conflict-deadlock.md`'s ruling: additive reporting,
    never a loosened refusal). A bare `claim_held_by_me` call collapses (i)
    and (iii) into one refusal and would orphan the frontmatter stamp
    forever in case (i) — this is why the dir's existence is checked
    separately rather than trusting the predicate's own `False` for
    "absent" to mean "not mine" (it means both, indistinguishably).
    `resolved_sid` is only passed to `claim_held_by_me` as `my_sid` when it
    came from an explicit `--session-id` or a warm-carried identity;
    otherwise the call omits `my_sid` so the predicate's own fail-closed
    ambient-env path governs (an explicit `my_sid` would short-circuit that
    guard, which is not warranted for the plain env-fallback case — see
    `liveness.claim_held_by_me`'s own docstring). A brief-stage claim never
    reaches this gate: no frontmatter stamp was ever written for it, so
    there is nothing here for a holder gate to protect.

    ORDERING (2026-08-30): `release_artifact`'s own docstring states an
    ORDERING CONTRACT — the caller reverts the frontmatter stamp BEFORE
    calling it, so a crash lands on the recoverable "open but claim-held"
    state rather than the re-admitting one. The non-brief tail below now
    calls the class inverse (`cs_unclaim_handoff`/`cs_release_memo_revert`)
    FIRST and `release_artifact` second, matching that contract — `drop`
    used to call them in the opposite order. `released` therefore reads
    `False` on both `APPLY_EXIT_PARTIAL_MUTATION` returns (the class
    inverse failed before `release_artifact` ever ran) and `True` only on
    the terminal `APPLY_EXIT_OK`, replacing the prior hardcoded `True` on
    every return regardless of what actually landed
    (`state/lessons/2026-08-27-the-committer-reports-blocked-after-it-
    committed.md`'s report/ground-truth family — this is the mirror-image
    instance: claiming a mutation that did not happen, not denying one that
    did).

    Never conditioned on `directives[].already_satisfied` or any other
    `apply`-side bookkeeping — `drop` recomputes nothing from a prior run and
    composes both primitives every time it is invoked, for either class of
    artifact `session-claim-cli claim-artifact` can target. The handoff-
    transition primitive is invoked only for `class_ == "handoff"`; the
    memo-transition inverse (`cs_release_memo_revert`, C8 BUILD (4)) is
    invoked only for `class_ == "memo"` — each class inverts its OWN
    claim-stamp write, never the other's.

    BRIEF-STAGE DROP IS LOCK-RELEASE ONLY. When the claim being dropped is
    still at `brief` stage — reserved by `brief`, never promoted by an `apply`
    — no frontmatter stamp was ever written, so neither class inverse is
    invoked and `unclaimed` reports `None` alongside `claim_stage: "brief"`.
    This is NOT a widening of `handoff_transition._unclaim`, which keeps its
    in_flight -> ready_to_fire-only, fail-loud contract untouched; it is
    simply not asking it to invert a write that never happened.

    PUT-DOWN CLEARS, REPARK LEAVES (C7 Part A design point): `drop` is the
    put-down path and always reverts the frontmatter claim-stamp
    (`claimed_by`/`claimed_at`) back to unclaimed. `picked_up_by` is NOT
    among them and survives the drop — the enumeration above read
    `picked_up_by` until 2026-08-30 and overstated the primitive. Repark
    (`handoff_transition._repark`) is a DIFFERENT verb this function never
    calls — it deliberately leaves the stamp intact so the claim hands
    onward. Nothing here needs to special-case repark; it simply is not
    `drop`.
    """
    root = repo_root or _resolve_repo_root_for_apply()
    if root is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": "could not resolve a git worktree root"}

    resolved_sid = _resolve_explicit_session_id(session_id)
    if resolved_sid is None:
        return APPLY_EXIT_TRANSPORT_FAIL, {
            "error": (
                "no session id resolvable via --session-id or "
                f"{'/'.join(_SESSION_ENV_READ_ORDER)} — refusing the ambient "
                "tier-4 sentinel (AC9(a))"
            ),
        }

    # D-G's containment denial, on the INVERSE verb. `apply` gained this
    # (chunk C9) and `drop` did not, so a foreign-repo drop raised an
    # uncaught `OutOfRepoPath` out of the release/transition primitives
    # instead of returning the named denial — a stack trace where the
    # forward verb gives a reason. Same bound, same reason string, same
    # exit code; a cross-repo memo in THIS repo's own `cross-repo/inbox/`
    # resolves under `root` and is unaffected, since the denial keys on
    # containment, never on the string "cross-repo" in a path.
    #
    # Hoisted to BEFORE `resolve_artifact` (2026-08-30), on the PASSED
    # `artifact_path` rather than the resolved one: once `resolve_artifact`
    # normalizes an absolute path itself, it raises `OutOfRepoPath` before
    # this function ever reached its own post-resolve check, which would
    # have surfaced as an uncaught exception instead of the named denial.
    # Still placed BEFORE `_session_identity` and both primitives: a drop
    # that is going to be denied must release nothing and stamp nothing, so
    # the denial cannot be reached with the claim already half-released.
    if artifact_path:
        try:
            _assert_in_repo_root(Path(artifact_path), root)
        except OutOfRepoPath:
            return APPLY_EXIT_CLAIM_DENIED, {
                "reason": "cross_repo_drop_denied",
                "error": (
                    f"{artifact_path}: not under this session's repo root "
                    f"({root}) — drop denied"
                ),
                "unclaimed": None,
            }

    try:
        artifact = resolve_artifact(artifact_path, root)
    except _ArtifactUnreadable as exc:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)}

    try:
        class_, basename = _class_and_basename(artifact)
    except _UnresolvableArtifactClass as exc:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc)}
    artifact_path_value = artifact.get("path", "") or artifact_path

    with _session_identity(resolved_sid):
        # Read the stage BEFORE releasing — `release_artifact` removes the
        # dir the stage file lives in.
        #
        # A `brief`-stage claim has no frontmatter stamp behind it: the
        # reservation was taken so the read-verify-draft window would be
        # excluded, and `apply` (which is what stamps) never ran. Dropping it
        # is therefore lock-release ONLY. Calling the class inverse anyway
        # would hand `handoff_transition._unclaim` a `deployment_state` that
        # is not `in_flight`, which it correctly fail-louds on (exit 1, no
        # write) — turning an ordinary "briefed it, changed my mind" into a
        # partial-mutation exit. Widening `_unclaim` to tolerate that is NOT
        # the fix; not calling it is.
        claims_dir = root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
        claim_is_brief_stage = claim_stage(claims_dir) == CLAIM_STAGE_BRIEF

        # A brief-stage LOCK beside an apply-stage FRONTMATTER STAMP is not a
        # brief-stage claim, and taking the lock-only arm on one is how `drop`
        # came to report `released: true` while leaving a baton reading
        # `status: claimed` / `deployment_state: in_flight` on disk, with the
        # session-claim ledger empty. Two planes disagreed and the cheap one won.
        #
        # It is reachable because `archive-stamp-cli claim-handoff` writes the
        # frontmatter stamp WITHOUT promoting the claim stage, so the lock says
        # brief and the artifact says claimed. That verb is being fixed
        # separately; this arm must not depend on it having been, because the
        # failure here is silent and the operator's only signal is the word
        # `released`.
        #
        # The stamp is the authority: if one is present the claim is apply-stage
        # whatever the lock dir says, so fall through to the holder-gated path
        # that actually unstamps it.
        if claim_is_brief_stage and artifact_path_value:
            try:
                from coordinator_core.ops.read_frontmatter_field import (
                    read_frontmatter_field,
                )

                _fm_holder = read_frontmatter_field(artifact_path_value, "claimed_by")
            except Exception:  # noqa: BLE001 -- an unreadable artifact keeps prior behaviour
                _fm_holder = ""
            if (_fm_holder or "").strip():
                claim_is_brief_stage = False

        if claim_is_brief_stage:
            # No frontmatter stamp was ever written for a brief-stage claim
            # — nothing here for a holder gate to protect. Lock-release only,
            # unchanged.
            # `release_artifact` is itself holder-gated and NO-OPs for a
            # non-holder, so a hardcoded `"released": True` here claims a
            # write that did not happen — the same report/ground-truth family
            # the holder gate below fixes, on the one arm that gate exempts.
            # Report what the primitive actually returned.
            _released = release_artifact(class_, basename, cwd=str(root))
            return APPLY_EXIT_OK, {
                "class": class_,
                "basename": basename,
                "released": bool(_released),
                "unclaimed": None,
                "claim_stage": CLAIM_STAGE_BRIEF,
                "commit_sha": None,
            }

        # HOLDER GATE (2026-08-30) — ahead of both composed primitives and
        # ahead of `_scoped_commit`. Read the ledger dir ONCE and trichotomize:
        # (i) absent -> proceed (nothing to protect; this is the reaped/
        # crash-truncated state this call exists to clear), (ii) held by me
        # -> proceed, (iii) held by someone else -> refuse, naming the
        # recorded holder. A bare `claim_held_by_me` call collapses (i) and
        # (iii) into one refusal and would orphan the frontmatter stamp
        # forever in case (i) — do not collapse them.
        if claims_dir.is_dir():
            # `resolved_sid` is only trustworthy as an explicit `my_sid` when
            # it came from an explicit `--session-id` or a warm-carried
            # identity; the plain ambient-env fallback must let
            # `claim_held_by_me`'s own fail-closed path govern instead (see
            # its docstring) — an explicit `my_sid` would short-circuit that
            # guard, which is not warranted for the env-fallback case.
            #
            # WHAT THIS GATE IS NOT: an authorization boundary. An explicit
            # `--session-id` is taken on the caller's word — that is
            # `claim_held_by_me`'s ratified contract ("a caller that has
            # already resolved identity under its own rules is trusted with
            # it"), not a hole this gate opened, and nothing in the fleet
            # authenticates a session id. So the gate stops the ACCIDENTAL
            # non-holder drop, which is the defect it was built for, and a
            # caller asserting the holder's id passes it. Do not cite it as
            # an authorization boundary, and do not make this one call site
            # authenticate while every other `claim_held_by_me` consumer
            # keeps the contract.
            #
            # What IS owed is telling the two apart downstream, per the
            # additive-reporting ruling in `docs/wiki/claim-conflict-
            # deadlock.md`: `identity_source` below labels which identity the
            # gate decided on, so a decision made on a caller-ASSERTED id is
            # distinguishable from one made on a RESOLVED id. It labels the
            # discriminator already computed here and never selects an
            # identity — a second ladder walk is what
            # `test_warm_identity_env_reads.LADDER_READ_EXEMPTIONS` exists to
            # keep out of this tree.
            _sid_is_explicit_or_warm = bool(session_id) or in_warm_served_request()
            identity_source = (
                "explicit-session-id"
                if session_id
                else "warm-carried"
                if _sid_is_explicit_or_warm
                else "ambient-env"
            )
            if _sid_is_explicit_or_warm:
                held_by_me = _liveness.claim_held_by_me(
                    str(claims_dir), my_sid=resolved_sid, cwd=str(root)
                )
            else:
                held_by_me = _liveness.claim_held_by_me(str(claims_dir), cwd=str(root))
            if not held_by_me:
                recorded_holder = _recorded_claim_holder(claims_dir)
                return APPLY_EXIT_CLAIM_DENIED, {
                    "reason": "drop_not_holder",
                    "error": (
                        f"{artifact_path_value}: claimed by {recorded_holder!r}, "
                        "not this session — drop denied"
                    ),
                    "class": class_,
                    "basename": basename,
                    "released": False,
                    "unclaimed": None,
                    "identity_source": identity_source,
                }

        unclaimed: Optional[bool] = None
        # C13/DR-273 — memo release now commits its own terminal write; when
        # non-None this is the SHA that landed, used below as the fallback
        # `_scoped_commit` reports when its own pathspec-diff finds nothing
        # dirty (the write was already committed by cs_release_memo_revert).
        memo_op_commit_sha: Optional[str] = None
        # ORDERING (2026-08-30): the class inverse runs BEFORE
        # `release_artifact`, per `release_artifact`'s own ORDERING CONTRACT
        # docstring — the caller reverts the frontmatter stamp first, so a
        # crash lands on the recoverable "open but claim-held" state rather
        # than the re-admitting one. `released` is `False` on both partial-
        # mutation returns below because `release_artifact` has not run yet.
        if class_ == "handoff":
            resolved_handoff_path = _assert_in_repo_root(Path(artifact_path_value), root)
            unclaimed = _normalize_primitive_result(cs_unclaim_handoff(str(resolved_handoff_path)))
            if not unclaimed:
                return APPLY_EXIT_PARTIAL_MUTATION, {
                    "error": f"drop: cs_unclaim_handoff failed for {artifact_path_value}",
                    "class": class_,
                    "basename": basename,
                    "released": False,
                    "unclaimed": False,
                }
        elif class_ == "memo":
            resolved_memo_path = _assert_in_repo_root(Path(artifact_path_value), root)
            release_result = cs_release_memo_revert(str(resolved_memo_path), return_result=True)
            unclaimed = _normalize_primitive_result(release_result["exit_code"])
            memo_op_commit_sha = release_result.get("commit_sha")
            if not unclaimed:
                return APPLY_EXIT_PARTIAL_MUTATION, {
                    "error": f"drop: cs_release_memo_revert failed for {artifact_path_value}",
                    "class": class_,
                    "basename": basename,
                    "released": False,
                    "unclaimed": False,
                }

        # `release_artifact` always returns True (no-op success on every
        # non-holder / already-absent path — see its own docstring); nothing
        # here branches on its return value.
        release_artifact(class_, basename, cwd=str(root))

        try:
            scoped_sha = _scoped_commit(root, artifact_path_value, class_, basename, ["drop"])
        except RuntimeError as exc:
            # Both the frontmatter revert (class inverse, above) and the
            # ledger release (`release_artifact`, immediately above) have
            # already landed by this point — only the terminal commit
            # failed. `released`/`unclaimed` therefore report what actually
            # happened (both True) rather than the `False` shape the two
            # earlier partial-mutation returns use; those sit ABOVE
            # `release_artifact` and are correct for their position, this
            # one sits below it.
            return APPLY_EXIT_PARTIAL_MUTATION, {
                "error": f"drop: _scoped_commit failed for {artifact_path_value}: {exc}",
                "class": class_,
                "basename": basename,
                "released": True,
                "unclaimed": unclaimed,
                "commit_sha": None,
            }
        commit_sha = scoped_sha if scoped_sha is not None else memo_op_commit_sha

    return APPLY_EXIT_OK, {
        "class": class_,
        "basename": basename,
        "released": True,
        "unclaimed": unclaimed,
        "claim_stage": CLAIM_STAGE_APPLY,
        "commit_sha": commit_sha,
    }


def _usage_drop(prog: str) -> int:
    print(f"usage: {prog} drop <artifact-path> [--session-id <id>]", file=sys.stderr)
    return APPLY_EXIT_TRANSPORT_FAIL


def main_drop(argv: list[str]) -> int:
    """`main()`'s `drop` dispatch arm — parses argv, calls `drop()`, prints
    the report, returns its exit code. Mirrors `main_apply`'s argv shape
    minus `--decisions` — `drop` never gates on a judgment point, so there is
    nothing for a disposition to resolve."""
    if not argv:
        return _usage_drop("pickup-assemble")

    artifact_path = argv[0]
    reject = _reject_if_multi_artifact(artifact_path, "drop", "pickup-assemble")
    if reject is not None:
        return reject
    tail = argv[1:]
    session_id: Optional[str] = None
    i = 0
    while i < len(tail):
        tok = tail[i]
        if tok == "--session-id":
            if i + 1 >= len(tail):
                return _usage_drop("pickup-assemble")
            session_id = tail[i + 1]
            i += 2
        else:
            print(f"pickup-assemble drop: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage_drop("pickup-assemble")

    exit_code, report = drop(artifact_path, session_id=session_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code
