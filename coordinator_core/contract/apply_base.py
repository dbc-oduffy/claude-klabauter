"""coordinator_core.contract.apply_base — the shared Tier-B apply runner
every mutating computed-skill assembler's own `apply()` composes (DR-092's
named un-defer trigger).

Purpose: factors the domain-agnostic mutating-half machinery
`pickup_assemble/apply.py` first proved out — directive-dependency
ordering, per-directive judgment-point gating through a closed dispatch
table, session-identity propagation, in-repo path safety, and the
pathspec-scoped commit discipline that survives a shared concurrent-EM
working tree — into ONE module every apply half composes rather than
re-derives. `baton_assemble` / `merge_assemble` / `consolidate_assemble`
(B4 W0/W2/W3) consume this from the start; `pickup_assemble/apply.py` is
refactored (chunk C2) to consume it too, becoming the first of four
consumers rather than the donor that keeps its own copy.

What stays per-consumer (deliberately NOT here): the closed CLI dispatch
table and its handler bodies (each assembler's own executable universe —
`resolve_cli`/`execute_directives` take that table as a parameter, never
hardcode one), the top-level `apply()`/`drop()` orchestration
(brief-recompute, claim-grant resolution, artifact classification), and
any consumer-specific mutating primitive (`_run_git`, archive-stamp
calls, etc. — `scoped_commit` takes a `run_git` callable, never shells
out itself).

Contract: DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C2

DR-092 (ACCEPTED): un-defers this module once a second real apply/dispatch
half exists — B4's `baton_assemble` is that named trigger. `apply_base` is
PROVISIONAL through W3, not frozen at this landing: `merge_assemble` (C6)
and `consolidate_assemble` (C8) are explicitly AUTHORIZED to feed their
own real divergence BACK INTO this module's parameters (widen the shared
runner) rather than special-casing at their own call site — a call-site
special-case bolted onto the shared runner is the exact DR-092
anti-pattern this factoring exists to avoid. Generality is confirmed only
once ALL FOUR consumers (pickup, baton, merge, consolidate) land green,
not after pickup alone.

Negative-spec:
    - Do NOT add a consumer-specific CLI name or handler here — the
      dispatch table is always supplied by the caller (`resolve_cli`'s
      `dispatch_table` parameter, `execute_directives`'s
      `dispatch_table` parameter); this module never hardcodes a domain
      verb.
    - Do NOT call `subprocess`/shell directly here — `scoped_commit`
      takes an injected `run_git` callable; this module has no git
      opinion of its own beyond the pathspec discipline. Its `add`/
      `commit` invocations are each wrapped in
      `coordinator_core.git_lock_retry.run_with_lock_retry`, which is
      itself callable-injected (takes a zero-arg closure over `run_git`)
      and does no shelling-out of its own — this module still never
      shells out, directly or otherwise.
    - Do NOT read `judgment_points[].recommendation` anywhere in
      `execute_directives`'s control flow — a directive only ever fires
      off an EXPLICIT `decisions[jp_id].disposition` whose OWN
      `resolves` list names it (`disposition_resolves_directive`);
      `recommendation` is advisory content for a human/EM reader, never
      a control-flow input.
    - Do NOT special-case a consumer's real divergence at its own call
      site once that consumer has landed here — feed the divergence back
      into this module's own parameters (DR-092; see C6/C8's
      authorization to widen, not bolt on).
    - Do NOT reintroduce a per-item `add`/`commit` loop into
      `scoped_commit` — it takes exactly ONE `artifact_rel_path` and stays
      O(1) at 2 `.git/index.lock` acquisitions per call BY CONSTRUCTION
      (one `add`, one `commit`, both `run_with_lock_retry`-wrapped),
      never scaling with a caller-side item count (AC-7,
      archive/specs/2026-08/2026-08-13-commit-seams-inherit-lock-reap-and-retry.md
      chunk C5 — the burst-source table's `contract/apply_base.py` row is
      already O(1) and has no further reduction available; a caller
      needing to commit N items loops OUTSIDE this function, calling it
      once per artifact, and is the one responsible for its own
      acquisition-count discipline at that call site — see
      `backlog_grind_assemble/apply.py::_dispatch_commit_per_item`'s own
      `_stage_paths` pre-pass for that pattern). Pinned by
      `coordinator_core/contract/tests/test_apply_base_lock_acquisition_pin.py`.
    - Do NOT build a general transaction manager, two-phase commit, or
      journal here — `compensators` (2026-07-29, `baton_assemble`'s
      orphaned-scaffold finding) is a single, narrow reaction to
      `APPLY_EXIT_PARTIAL_MUTATION` only: run each landed directive's own
      registered undo, in reverse landing order, never a generalized
      rollback engine. See `execute_directives`'s own docstring for the
      full contract (opt-in, additive, byte-identical when omitted).

COMPOSITION BUDGET WIRING (chunk C10, docs/plans/2026-08-15-composition-
invocation-budgets.md): `execute_directives`'s optional `composition_budget`
parameter (`coordinator_core.composition_budget.CompositionBudget | None`,
default `None` — byte-identical to pre-C10 behaviour when omitted) adds
TWO boundary checks and ONE mid-directive advisory, never a mid-mutation
abort (the anti-scope's hard line):

    - BEFORE-FIRST-MUTATION boundary: checked once, after the whole-list
      `resolve_cli` pre-validation pass and the pre-loop claim-grant gate,
      and BEFORE the per-directive loop begins. Nothing has mutated by
      this point regardless of what the loop's first entries look like —
      this is why the check sits before the loop rather than "at the
      first iteration": an `already_satisfied` directive at index 0 does
      not mutate either, so gating on "first iteration" would not in fact
      be "first mutation" (the caveat this chunk's brief names). A breach
      here reuses `APPLY_EXIT_CLAIM_DENIED` (position 2) rather than
      minting a new ladder member — nothing landed yet, `"landed": []`,
      the exact shape the pre-existing claim-grant-denied branch already
      returns; a caller switching on `rc == 2` sees the same "nothing
      mutated, consult the report" contract it already handles. The
      report's additive `"budget_breach"` key (present only when this
      fires) carries the breach message for a caller that wants to tell
      the two `CLAIM_DENIED` causes apart.
    - AFTER-LAST-MUTATION boundary: checked once, after the loop
      completes (successfully — this call site is never reached from the
      `except Exception` partial-mutation branch, so it structurally
      cannot precede or trigger `_run_compensators`). By this point every
      directive that was going to mutate already has, successfully — a
      breach here reports rc=0 with loud stderr (`APPLY_EXIT_OK`/
      `APPLY_EXIT_HALTED_AT_JUDGMENT`, whichever the loop's own outcome
      already was, UNCHANGED), never `APPLY_EXIT_PARTIAL_MUTATION`.
      Reusing `PARTIAL_MUTATION=4` here was considered and rejected: that
      code is the one that triggers `_run_compensators` in reverse
      landing order, so a post-loop breach on a run that completed
      successfully but slowly would tear down a run that had nothing
      wrong with it beyond taking too long — exactly the half-applied-
      ceremony harm this budget exists to prevent, caused by the guard
      itself. The report's additive `"budget_breach"` key carries the
      breach message here too.
    - MID-DIRECTIVE advisory: after each directive that actually
      dispatches a handler (never for `already_satisfied` entries — they
      did no work this run), `composition_budget.advisory_check()` is
      called and `record_invocation()` records the dispatch.
      `advisory_check()` never raises and has no control-flow effect
      (§ `composition_budget.py`'s own contract) — a breach here only
      emits loud stderr, so a slow directive's cost surfaces right after
      IT finishes rather than only at the run's final report. This is
      coarser than "inside" a single long-running handler (this module
      does not own handler internals to instrument further), but it is
      the finest granularity available at this seam without reaching
      into a composed primitive's own body — which the anti-scope's
      mid-mutation-abort prohibition would forbid instrumenting for
      abort purposes anyway.

ARMED (chunk C2, docs/plans/2026-08-18-arm-the-composition-budget.md): all
five composition apply entries this module serves — `backlog_grind_
assemble`, `baton_assemble`, `consolidate_assemble`, `merge_assemble`,
`pickup_assemble` — now construct a fresh `CompositionBudget` per apply run
via `coordinator_core.telemetry.composition_record.make_fleet_budget` and
pass it through as `composition_budget`, never `None`; the two wrapper
callers (`baton_assemble`, `pickup_assemble`) thread it through their own
`_execute_directives` wrapper rather than constructing it there, so its
lifetime is the apply run's, not the wrapper's. Each call site also wraps
its `execute_directives` call in a `try/finally` that calls
`flush_composition_record` exactly once, whatever the run's outcome. A
caller passing `None` explicitly (or, before this chunk, one that never
threaded the parameter at all) still gets the byte-identical pre-C10
behaviour described above — the fleet factory's ceilings themselves start
at `None` too (pure recorder; C5 arms them from what this wiring records).

SESSION IDENTITY SHAPE (chunk C6, docs/plans/2026-08-15-warm-engine-retires-
the-per-invocation-cold-start.md): `session_identity()` is a per-CONTEXT
`contextvars.ContextVar` scope, not a process-wide `os.environ` mutation.
Under a warm engine, two overlapping in-process dispatches (two threads, or
two `asyncio` tasks) each hold their OWN session id for the lifetime of
their own `with session_identity(...):` block — entering or exiting one
never overwrites the other's ambient identity, and neither block leaves a
DEAD session id behind as a process-wide default once it exits. This is the
shape C11/C12 (concurrent MUTATING dispatch) build on: a per-request
contextvar, not a global.

`os.environ` is mirrored from the active contextvar **only** at the ONE
outermost boundary this module owns where a CHILD PROCESS must inherit the
identity — `scoped_commit`'s own `run_git` invocations, the sole
subprocess-spawning seam here (`git add`/`diff --cached`/`commit`/
`rev-parse`, each already wrapped by `run_with_lock_retry` where
applicable). `_mirror_session_env_for_subprocess` performs that mirror,
scoped to exactly the one `run_git` call it wraps, restoring the prior
`os.environ` value (or deleting the key) immediately after — never left
mirrored in between calls, and never mirrored anywhere else in this module.
A caller with no active `session_identity()` context (the contextvar reads
its `None` default) leaves `os.environ` untouched, matching today's
behaviour for a dispatch that never entered a session-identity block at
all.

    INSTRUMENTATION-ERROR ISOLATION: `op_latency`, `_hook_envelope`, and
    `cli_entry._record` are all deliberately unable to fail at their own
    seams (an observed pattern in this codebase, not a ruled house rule —
    no decision record was found asserting it). A budget BREACH must be
    loud (that is the whole point of `disposition="fail-loud"`); an
    INSTRUMENTATION ERROR — an exception from a caller-supplied `on_count`
    callback, or any other unexpected failure inside the budget machinery
    itself, as opposed to `BudgetBreach` — must not. `_budget_call` below
    resolves that tension: it lets `BudgetBreach` (and only `BudgetBreach`)
    propagate to its caller, and swallows any OTHER exception into a loud
    stderr line, so a bug in an injected `on_count` sink can never take
    down a ceremony close.
"""
from __future__ import annotations

import contextvars
import dataclasses
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional

from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.composition_budget import BudgetBreach, CompositionBudget
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.session import core as session_core

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exit-code contract — shared by every apply/dispatch half. Locally scoped
# to the mutating half (never inherited from a compute-only `brief`'s own
# exit codes, which each consumer defines separately per
# `computed-skills.md` § Exit-code contract).
# ---------------------------------------------------------------------------
APPLY_EXIT_OK = 0
APPLY_EXIT_HALTED_AT_JUDGMENT = 1
APPLY_EXIT_CLAIM_DENIED = 2
APPLY_EXIT_TRANSPORT_FAIL = 3
APPLY_EXIT_PARTIAL_MUTATION = 4

GRANTED_VERDICTS = frozenset({"granted", "granted-with-warning"})


class UnrecognizedDirective(Exception):
    """Raised by `resolve_cli` for a `cli` name outside the caller's own
    closed dispatch table — the run aborts before any directive in it
    executes ("mutates nothing" means the WHOLE run aborts
    pre-validation, not merely the one bad directive)."""


class OutOfRepoPath(Exception):
    """Raised when a directive-derived path resolves outside `repo_root`
    — asserted before any mutation."""


class NoResolvableSessionId(Exception):
    """Raised when neither an explicit session id nor any entry of the
    caller's env-read-order resolves one. Callers refuse to fall through
    to an ambient tier-4 sentinel file under concurrency ambiguity."""


class DirectiveDependencyCycle(Exception):
    """Raised by `order_by_depends_on` when `directives[].depends_on`
    forms a cycle among directive ids. Defensive — no known assembler
    path produces one — but a silent infinite-stall is worse than a loud
    one."""


@dataclasses.dataclass(frozen=True)
class DirectiveResult:
    """ONE normalized shape for what happened to a single directive,
    whether it actually dispatched or was skipped as
    `already_satisfied`."""

    directive_id: str
    already_satisfied: bool
    detail: Optional[dict[str, Any]]

    def to_report(self) -> dict[str, Any]:
        return {
            "id": self.directive_id,
            "already_satisfied": self.already_satisfied,
            "detail": self.detail,
        }


def normalize_primitive_result(value: Any) -> bool:
    """Normalizes the two return conventions a composed mutating
    primitive may use into ONE meaning: `True` iff the call succeeded.
    Some primitives return `bool` (`True` == success); others return a
    POSIX-style `int` exit code (`0` == success). Reading an `int` result
    as a bare truthy value inverts a successful `0` into falsy — exactly
    the asymmetry this function normalizes in ONE place rather than
    trusting every call site's own `if`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 0
    raise TypeError(f"unrecognized primitive result type {type(value)!r}")


def normalize_depends_on(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def judgment_points_by_id(judgment_points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {jp["id"]: jp for jp in judgment_points if jp.get("id")}


def disposition_resolves_directive(
    jp: dict[str, Any], decisions: dict[str, Any], directive_id: str
) -> bool:
    """"has a disposition been set on `jp`" is NOT sufficient to fire
    `directive_id` — every judgment point encodes terminal-vs-non-terminal
    in each disposition's OWN `resolves` list. `directive_id` is
    resolved-to-fire iff `decisions[jp['id']].disposition` names a
    disposition on `jp` whose own `resolves` includes `directive_id` —
    never merely "some disposition was picked" (the Director of Engineering v2 finding-1
    value-aware predicate, pickup_assemble's chunk C7 Part B)."""
    entry = decisions.get(jp.get("id")) if jp.get("id") else None
    if not isinstance(entry, dict):
        return False
    chosen = entry.get("disposition")
    if not chosen:
        return False
    for candidate in jp.get("dispositions", []) or []:
        if candidate.get("value") == chosen:
            return directive_id in (candidate.get("resolves") or [])
    return False


def directive_gate_open(
    directive: dict[str, Any],
    jp_by_id: dict[str, dict[str, Any]],
    decisions: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Per-directive judgment-halt: a directive is ready to fire when
    every judgment-point id its `depends_on` names is resolved to a
    disposition whose own `resolves` list includes THIS directive's id.
    A `depends_on` value that does not name a live entry in `jp_by_id`
    (a directive id, for `order_by_depends_on`'s own directive-to-
    directive ordering, or a judgment point already absent from this run)
    never gates here. Returns `(ready, blocking_judgment_point_ids)`."""
    blocking: list[str] = []
    for dep in normalize_depends_on(directive.get("depends_on")):
        jp = jp_by_id.get(dep)
        if jp is None:
            continue
        if not disposition_resolves_directive(jp, decisions, directive["id"]):
            blocking.append(dep)
    return not blocking, blocking


def order_by_depends_on(directives: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable topological sort on `directives[].depends_on` — a directive
    never dispatches before every directive id it names. A `depends_on`
    value naming something OTHER than a directive in this same list (e.g.
    a judgment-point id left on the dict for a branch that never reaches
    this function because `judgment_points` is already empty by the time
    it runs) is treated as already-resolved and ignored — it is never an
    unmet dependency here. Ties break on the directives' original list
    order, so a run's dispatch order is deterministic and
    partial-mutation reporting is reproducible."""
    by_id = {d["id"]: d for d in directives}
    deps: dict[str, list[str]] = {
        d["id"]: [dep for dep in normalize_depends_on(d.get("depends_on")) if dep in by_id]
        for d in directives
    }
    remaining = {d["id"] for d in directives}
    ordered: list[dict[str, Any]] = []
    ordered_ids: set[str] = set()
    progress = True
    while remaining and progress:
        progress = False
        for d in directives:
            did = d["id"]
            if did in remaining and all(dep in ordered_ids for dep in deps[did]):
                ordered.append(d)
                ordered_ids.add(did)
                remaining.discard(did)
                progress = True
    if remaining:
        raise DirectiveDependencyCycle(sorted(remaining))
    return ordered


def assert_in_repo_root(candidate: Path, repo_root: Path) -> Path:
    """Resolves `candidate` and asserts it sits inside `repo_root`. Raises
    `OutOfRepoPath` otherwise. Every dispatch handler that touches a real
    filesystem path derived from `directives[].args` calls this before
    handing the resolved path to a mutating primitive."""
    resolved_root = repo_root.resolve()
    resolved = candidate if candidate.is_absolute() else (repo_root / candidate)
    resolved = resolved.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise OutOfRepoPath(f"{candidate} resolves outside repo root {repo_root}") from None
    return resolved


def reject_path_traversal(value: str, *, label: str) -> str:
    """Defense-in-depth on a directive-derived identifier that is never
    itself joined into a path by the caller here but IS handed to a
    composed primitive that builds one internally (e.g. a claim-dir
    basename) — a `/` or `..` segment has no legitimate value in this
    slot."""
    if not value or "/" in value or "\\" in value or value in (".", ".."):
        raise OutOfRepoPath(f"{label} {value!r} is not a bare path segment")
    return value


def resolve_cli(
    dispatch_table: dict[str, Callable[[list[str], Path], dict[str, Any]]],
    cli_name: str,
) -> Callable[[list[str], Path], dict[str, Any]]:
    """The one seam `directives[].cli` ever passes through, for whichever
    closed `dispatch_table` the caller owns. Closed over a literal dict
    supplied by the caller — an unrecognized name raises before any
    directive in the run has executed."""
    handler = dispatch_table.get(cli_name)
    if handler is None:
        raise UnrecognizedDirective(f"unrecognized directive cli {cli_name!r}")
    return handler


def assert_dispatchable(assembler_name: str, verb: str) -> None:
    """The ONE shared admission check against
    `coordinator_core.authz.dispatchable.ASSEMBLER_DISPATCHABLE` — `resolve_op`
    below calls it, and `workday_complete`/`workstream_complete`/`workweek_complete`'s
    own private `_resolve_cli` functions (C7) call it too, so the membership check
    is never hand-copied into three private bodies (DR-092's own negative-spec
    against special-casing a consumer's divergence at its own call site instead of
    feeding it back into this module's params).

    Raises `UnrecognizedDirective` when `verb` is absent from `assembler_name`'s
    own entry in `ASSEMBLER_DISPATCHABLE` — DEFAULT-DENY: an `assembler_name` with
    no entry at all denies every `verb`, same as an entry present but not
    containing `verb`."""
    allowed = ASSEMBLER_DISPATCHABLE.get(assembler_name, frozenset())
    if verb not in allowed:
        raise UnrecognizedDirective(
            f"{verb!r} is a registered op that {assembler_name!r} may not dispatch"
        )


def resolve_op(
    dispatch_table: dict[str, Callable[[list[str], Path], dict[str, Any]]],
    assembler_name: str,
    op_name: str,
) -> Callable[[list[str], Path], dict[str, Any]]:
    """A second pre-validated seam, `resolve_cli`'s exact refusal shape, for
    `directives[].op` — a lookup plus admission check, NEVER a generic
    `(params, repo_root) -> _invoke_op_in_process(...)` shim (§ apply_base module
    docstring's own negative-spec on hardcoding a domain verb applies equally
    here: this function never builds params from directive-supplied keys, and
    never reaches `_invoke_op_in_process` directly — it returns the SAME
    hand-written per-verb adapter `dispatch_table[op_name]` already names, exactly
    as `resolve_cli` returns the same adapter its own `_CLI_DISPATCH` entry names).

    Two refusal reasons, kept distinct in the raised message so a permission
    refusal never reads as a typo:
      (a) `op_name` has no adapter in `dispatch_table` at all — not a name this
          caller can dispatch, registered or not;
      (b) `op_name` has an adapter but `assembler_name` is not allowlisted for it
          in `ASSEMBLER_DISPATCHABLE` (`assert_dispatchable` above).

    Either refusal raises `UnrecognizedDirective` — same exception `resolve_cli`
    raises, so a caller's whole-list pre-validation pass fails the run before any
    directive executes, never degrading to inert or skip-and-continue."""
    handler = dispatch_table.get(op_name)
    if handler is None:
        raise UnrecognizedDirective(f"unrecognized directive op {op_name!r}")
    assert_dispatchable(assembler_name, op_name)
    return handler


# ---------------------------------------------------------------------------
# Session-id propagation — explicit only, never an ambient tier-4
# sentinel. `SESSION_ENV_VARS` names the two identity chains a resolved
# explicit id is scoped INTO for the duration of a call, via a
# `contextvars.ContextVar` per name (see `session_identity` below) — NOT
# `os.environ` process-wide, since chunk C6. `SESSION_ENV_READ_ORDER` is
# what an implicit id is read FROM, highest-precedence first, when no
# explicit id is supplied — that read is a genuine ambient-environment
# read (the caller's own launch environment), unrelated to the
# `session_identity` contextvar scope.
# ---------------------------------------------------------------------------
SESSION_ENV_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID")

SESSION_ENV_READ_ORDER = (
    "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def resolve_explicit_session_id(
    session_id: Optional[str],
    *,
    env_read_order: tuple[str, ...] = SESSION_ENV_READ_ORDER,
) -> Optional[str]:
    if session_id:
        return session_id
    for env_var in env_read_order:
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    return None


# One `ContextVar` per env-var NAME, created lazily and cached here so an
# arbitrary caller-supplied `env_vars` tuple (baton/pickup/etc. all pass the
# same two names today, but nothing pins that) still gets a stable, shared
# contextvar per name across every consumer of this module — never a fresh
# contextvar per call, which would make `current_session_env` unable to see
# a value `session_identity` set moments earlier in the same context.
_SESSION_ID_CONTEXTVARS: dict[str, "contextvars.ContextVar[Optional[str]]"] = {}


def _contextvar_for(var: str) -> "contextvars.ContextVar[Optional[str]]":
    cv = _SESSION_ID_CONTEXTVARS.get(var)
    if cv is None:
        cv = contextvars.ContextVar(f"apply_base_session_env__{var}", default=None)
        _SESSION_ID_CONTEXTVARS[var] = cv
    return cv


@contextmanager
def session_identity(
    session_id: str, *, env_vars: tuple[str, ...] = SESSION_ENV_VARS
) -> Iterator[None]:
    """Scopes `session_id` into `env_vars`' contextvars for the lifetime of
    this `with` block ONLY — never `os.environ` (§ module docstring
    "SESSION IDENTITY SHAPE"). Two overlapping calls (two threads/tasks,
    the warm-dispatch shape) each see their own value for the duration of
    their own block; neither can overwrite the other's ambient identity,
    and exiting one never leaves a dead id as a process-wide default."""
    tokens = [(_contextvar_for(var), _contextvar_for(var).set(session_id)) for var in env_vars]
    try:
        yield
    finally:
        for cv, token in tokens:
            cv.reset(token)


def current_session_env(env_vars: tuple[str, ...] = SESSION_ENV_VARS) -> dict[str, str]:
    """Reads the ACTIVE `session_identity()` context's ids, keyed by env-var
    name — for a caller about to cross the one outermost boundary where a
    CHILD PROCESS must inherit them (a subprocess spawn), never for a
    general-purpose ambient read. A var with no active context (or no
    `session_identity()` entered at all) is omitted, not `None`-valued."""
    result: dict[str, str] = {}
    for var in env_vars:
        val = _contextvar_for(var).get()
        if val is not None:
            result[var] = val
    return result


@contextmanager
def _mirror_session_env_for_subprocess(
    env_vars: tuple[str, ...] = SESSION_ENV_VARS,
) -> Iterator[None]:
    """THE outermost boundary (§ module docstring "SESSION IDENTITY
    SHAPE"): mirrors the active `session_identity()` contextvar scope into
    `os.environ` for the duration of ONE subprocess-spawning call only,
    restoring (or deleting) immediately after. `scoped_commit`'s own
    `run_git` invocations are the sole subprocess seam this module owns —
    nothing else in this module touches `os.environ` for session identity.
    A context with no active session id leaves `os.environ` untouched."""
    overrides = current_session_env(env_vars)
    if not overrides:
        yield
        return
    previous = {var: os.environ.get(var) for var in overrides}
    for var, value in overrides.items():
        os.environ[var] = value
    try:
        yield
    finally:
        for var, value in previous.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def _budget_call(label: str, fn: Callable[[], Any]) -> Any:
    """Runs one `composition_budget` call, letting `BudgetBreach` (the
    deliberate fail-loud signal) propagate unchanged while any OTHER
    exception — an instrumentation error, e.g. from a caller-injected
    `on_count` — is swallowed into a loud stderr line and never propagates
    (§ module docstring, "INSTRUMENTATION-ERROR ISOLATION"). `label`
    identifies which of the three call sites (pre-mutation boundary,
    post-mutation boundary, mid-directive advisory) failed, for the
    stderr line only — never for control flow."""
    try:
        return fn()
    except BudgetBreach:
        raise
    except Exception as exc:  # noqa: BLE001 - isolated, never propagated
        print(
            f"[composition-budget] instrumentation error at {label}: {exc}",
            file=sys.stderr,
        )
        return None


def _run_compensators(
    compensators: dict[str, Callable[[dict[str, Any], Path, Optional[dict[str, Any]]], Any]],
    results: list[DirectiveResult],
    directive_lookup: dict[str, dict[str, Any]],
    repo_root: Path,
) -> list[dict[str, Any]]:
    """Runs the REVERSE-landing-order compensator pass a `PARTIAL_MUTATION`
    abort triggers (see `execute_directives`'s own docstring addendum). Only
    `results` entries that actually dispatched a handler this run
    (`already_satisfied` excluded — nothing of this run's own making to
    undo) AND have a registered compensator are candidates. A compensator
    that itself raises is caught here and recorded as `succeeded: False` —
    it must never propagate, and never replaces the caller's own `error`/
    `failed_directive` fields, which this function's return value is
    additive to, never substitutes for.

    Wraps each compensator call in `_mirror_session_env_for_subprocess`,
    same as the per-directive handler dispatch in `execute_directives`'s
    own loop above (§ module docstring "SESSION IDENTITY SHAPE") — a
    compensator is itself a caller-supplied callable that may resolve
    session identity from `os.environ` and shell out (e.g.
    `_compensate_d1_scaffold`'s re-render of the SAME generator scaffolder
    `handler` above ran, which stamps an ambient-session-derived
    `authoring_session` field only when the identity is mirrored into
    `os.environ` at call time). Omitting this wrap here left the
    boundary-#1/mid-directive/boundary-#2 sites mirrored but this reaction
    path un-mirrored — the 2026-08-15 regression this wrap fixes: a
    compensator's re-render diverging from the actual on-disk file it
    produced moments earlier purely because the ambient identity it saw at
    mint time was no longer visible at compensate time, which silently
    masked a real compensator outcome (declined to delete, reported
    `succeeded: True` anyway) rather than the instrumentation-only
    isolation this module's budget wiring is scoped to.

    RETURN-VALUE CONTRACT (2026-08-16, downstream of ada42cb429f2): a
    compensator's own RETURN VALUE, not merely whether it raised, decides
    `succeeded`. Every compensator registered today (`_D1_COMPENSATORS` in
    `baton_assemble/apply.py`: `_compensate_d1_scaffold`,
    `_compensate_d5_release_claim`) returns `None` on every path, including
    its own early-return "nothing to do here" guards -- so `None` MUST
    continue to mean success; flipping that would turn every one of
    today's normal no-op runs into a false `succeeded: False` alarm, a
    worse defect than the one this fixes. An EXPLICIT `False` return is
    the one signal a compensator has to say "I ran, I deliberately chose
    NOT to act, and that decision was itself intact" -- recorded as
    `succeeded: False` (matching a raise's own `succeeded: False`, since
    neither compensated anything) but distinguished by an additive
    `"declined": True` key with no `"error"` key, so a caller reading this
    list can always tell "the compensator broke" (`error` present) apart
    from "the compensator ran fine and chose not to act" (`declined`
    present) -- the exact conflation `ada42cb429f2` cured one layer up for
    missing ambient session identity. `True`, or any other non-`False`
    return, is treated the same as `None` -- `succeeded: True` -- so a
    compensator that returns `{"ok": True}` or similar today is unaffected."""
    outcomes: list[dict[str, Any]] = []
    for result in reversed(results):
        if result.already_satisfied:
            continue
        compensator = compensators.get(result.directive_id)
        if compensator is None:
            continue
        outcome: dict[str, Any] = {"directive_id": result.directive_id, "attempted": True}
        try:
            with _mirror_session_env_for_subprocess():
                comp_result = compensator(
                    directive_lookup[result.directive_id], repo_root, result.detail
                )
            if comp_result is False:
                outcome["succeeded"] = False
                outcome["declined"] = True
            else:
                outcome["succeeded"] = True
        except Exception as exc:  # noqa: BLE001 - recorded, never propagated
            outcome["succeeded"] = False
            outcome["error"] = str(exc)
        outcomes.append(outcome)
    return outcomes


def execute_directives(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    repo_root: Path,
    dispatch_table: dict[str, Callable[[list[str], Path], dict[str, Any]]],
    *,
    decisions: Optional[dict[str, Any]] = None,
    resolve_claim_grant: Optional[Callable[[], dict[str, Any]]] = None,
    compensators: Optional[
        dict[str, Callable[[dict[str, Any], Path, Optional[dict[str, Any]]], Any]]
    ] = None,
    composition_budget: "Optional[CompositionBudget]" = None,
    assembler_name: Optional[str] = None,
) -> tuple[int, dict[str, Any]]:
    """THE directive-execution seam, callable directly with two
    `judgment_points` lists differing only in `recommendation` content to
    prove the scoped predicate: the executed-directive log and resulting
    on-disk state are identical whether or not a `recommendation` is
    present.

    The halt is PER-DIRECTIVE, not a blunt "any non-empty
    `judgment_points` halts everything before any directive executes"
    rule. A directive with `depends_on: None`, or whose every named
    judgment-point dependency is resolved to a disposition whose OWN
    `resolves` list names this directive (`disposition_resolves_
    directive` — a value-aware predicate, NOT a plain "has a disposition
    been set" check), fires. A directive whose `depends_on` names an
    unresolved (or non-terminally-resolved) judgment point does not — the
    run still reports `APPLY_EXIT_HALTED_AT_JUDGMENT` overall whenever at
    least one directive was blocked this way, but every OTHER directive
    that reached "ready" this pass still dispatches. The `recommendation`
    key on any entry is never read here.

    Directives with zero directives to consider at all (`directives ==
    []`) still fall back to the old blunt behaviour: a non-empty
    `judgment_points` with nothing to dispatch is unconditionally
    `APPLY_EXIT_HALTED_AT_JUDGMENT` — there is no directive for the
    per-directive predicate to differentiate.

    `resolve_claim_grant`, when supplied, is re-resolved immediately
    before any directive dispatches, UNCONDITIONALLY on
    `judgment_points`' contents — a pre-loop blanket DENIED gate every
    `depends_on: None` directive dispatches behind, never a judgment
    point itself.

    `compensators`, when supplied, is an OPTIONAL per-directive-id map
    (`directive_id -> (directive, repo_root, detail) -> Any`) — additive,
    opt-in, and byte-identical-when-omitted: a directive with no entry, or
    a caller that never passes this parameter at all, dispatches through
    the exact control flow this function had before compensators existed.
    Compensation is a REACTION to failure, never a step of a normal run:
    it fires ONLY from the one `except Exception` branch below (a raised
    handler, i.e. `APPLY_EXIT_PARTIAL_MUTATION`) — never on
    `APPLY_EXIT_OK`, and never on `APPLY_EXIT_HALTED_AT_JUDGMENT` (a
    judgment halt is a legitimate pause an operator resumes, not a
    failure; compensating it would destroy in-progress work). When it
    fires, every directive that actually `landed` this run (excluding
    `already_satisfied` entries — they mutated nothing this run, there is
    nothing of this run's own making to undo) runs its own registered
    compensator, in REVERSE landing order, via `_run_compensators`. A
    compensator that itself raises is caught and recorded under the
    report's additive `"compensation"` key — it never propagates, and
    never replaces or masks the original `error`/`failed_directive`,
    which are computed first and are unconditionally present exactly as
    they were before this parameter existed.

    A directive dict carrying a truthy `"advisory"` key follows the same
    additive-opt-in-byte-identical-when-omitted shape, on the directive
    itself rather than as a caller-supplied map (its construction site is
    the natural place to declare "this check never blocks the run", not a
    second parallel id-keyed parameter here): when its handler raises, the
    failure is recorded in the report's additive `"advisory_failures"`
    list (each entry `{"directive_id", "error"}` — AC4, silence is a worse
    defect than the false alarm being fixed) and the loop CONTINUES to the
    remaining directives instead of returning `APPLY_EXIT_PARTIAL_MUTATION`.
    An advisory failure never appends to `results`/`landed` — it did not
    land — and is carried forward into whichever report this call
    eventually returns: `APPLY_EXIT_OK`, `APPLY_EXIT_HALTED_AT_JUDGMENT`,
    or a LATER directive's own `APPLY_EXIT_PARTIAL_MUTATION` (an advisory
    failure earlier in the run must not be dropped just because a
    different, non-advisory directive fails afterward — the operator
    reconciling that partial mutation still needs the full picture).
    Because an advisory failure itself never reaches the `except` branch's
    `PARTIAL_MUTATION` return, it never reaches `_run_compensators` either
    (AC5: compensation is a reaction to a mutation partially landing, and
    an advisory check never blocked the run to begin with — tearing down
    already-landed directives over it would be its own new defect). A
    directive with no `"advisory"` key, or a run where nothing sets one,
    never populates `"advisory_failures"` at all — the key is omitted, not
    emitted empty, so every pre-existing report shape is unchanged (AC6).

    A directive carries EXACTLY ONE of `cli` or `op` (C3, AC9) -- both
    present, or neither present, is a whole-run pre-validation refusal
    (`UnrecognizedDirective`), same as an unrecognized `cli` name always
    was; the absent-`cli` refusal this replaces is not a regression, it is
    the same refusal restated to also catch the new both-present case. A
    `cli` directive still resolves via `resolve_cli(dispatch_table, ...)`;
    an `op` directive resolves via `resolve_op(dispatch_table,
    assembler_name, ...)`, gated by `assembler_name` (the caller's own
    identity for `ASSEMBLER_DISPATCHABLE`'s default-deny lookup -- `None`
    denies every `op` directive, since `ASSEMBLER_DISPATCHABLE.get(None,
    frozenset())` is always empty). This is ONE resolution pass with a
    two-valued key, not a second dispatch mechanism: whichever key a
    directive carries, its resolved handler is invoked identically below
    (`handler(directive.get("args", []), repo_root)`), and the two-value
    check happens once, over the whole directive list, before any of them
    execute -- exactly where the `resolve_cli` pre-validation already ran.

    Otherwise orders execution-ready directives by `depends_on`
    (directive-to-directive ordering — a judgment-point id in
    `depends_on` is not a member of the directive-id set and is
    therefore ignored by this ordering step; see `order_by_depends_on`),
    skips `already_satisfied` directives without dispatching their
    handler, and reports one `DirectiveResult` per directive that
    actually dispatched (or was skipped as `already_satisfied`) in
    `report["results"]` / `report["landed"]` — a directive blocked by an
    unresolved judgment point never appears in either.
    """
    decisions = decisions or {}
    jp_by_id = judgment_points_by_id(judgment_points)

    if not directives:
        if judgment_points:
            return APPLY_EXIT_HALTED_AT_JUDGMENT, {
                "unresolved_judgment_points": [jp.get("id") for jp in judgment_points],
                "landed": [],
            }
        return APPLY_EXIT_OK, {"landed": []}

    if resolve_claim_grant is not None:
        # Same outermost-boundary rationale as the per-directive handler
        # call below: `resolve_claim_grant` is a caller-injected zero-arg
        # closure that may itself resolve session identity from
        # `os.environ` and shell out (claim-mechanics probes).
        with _mirror_session_env_for_subprocess():
            claim_grant = resolve_claim_grant()
        if claim_grant.get("verdict") not in GRANTED_VERDICTS:
            return APPLY_EXIT_CLAIM_DENIED, {
                "claim_grant": claim_grant,
                "landed": [],
            }

    # Composition-budget boundary #1 -- before any mutation (§ module
    # docstring "COMPOSITION BUDGET WIRING"). Nothing has mutated yet
    # regardless of what the loop below finds at its own first entry
    # (an already_satisfied directive does not mutate either), so this
    # single pre-loop check is genuinely "before first mutation," not
    # merely "before first iteration."
    if composition_budget is not None:
        breach_message: Optional[str] = None
        try:
            within_budget = _budget_call(
                "pre-mutation boundary", lambda: composition_budget.check(unit="pre_mutation")
            )
        except BudgetBreach as exc:
            breach_message = str(exc)
        else:
            if within_budget is False:
                breach_message = (
                    "composition budget breach: composition="
                    f"{composition_budget.composition_id!r} unit='pre_mutation' "
                    "(skip-and-surface)"
                )
        if breach_message is not None:
            return APPLY_EXIT_CLAIM_DENIED, {
                "landed": [],
                "budget_breach": breach_message,
            }

    try:
        ordered = order_by_depends_on(directives)
    except DirectiveDependencyCycle as exc:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": f"depends_on cycle: {exc}", "landed": []}

    # Pre-validate the WHOLE directive list before executing any of them:
    # "mutates nothing" on an unrecognized cli means the run aborts
    # pre-emptively, not merely the offending directive — unaffected by
    # per-directive judgment gating, which only decides WHETHER a
    # structurally-valid directive dispatches this pass.
    try:
        resolved = []
        for d in ordered:
            has_cli = "cli" in d
            has_op = "op" in d
            if has_cli and has_op:
                raise UnrecognizedDirective(
                    f"directive {d['id']!r} carries both 'cli' and 'op' -- exactly one "
                    "is required"
                )
            if has_cli:
                handler = resolve_cli(dispatch_table, d["cli"])
            elif has_op:
                handler = resolve_op(dispatch_table, assembler_name, d["op"])
            else:
                raise UnrecognizedDirective(
                    f"directive {d['id']!r} carries neither 'cli' nor 'op'"
                )
            resolved.append((d["id"], handler, d))
    except UnrecognizedDirective as exc:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc), "landed": []}

    directive_lookup = {directive_id: d for directive_id, _, d in resolved}
    landed: list[str] = []
    results: list[DirectiveResult] = []
    blocked_jp_ids: set[str] = set()
    advisory_failures: list[dict[str, Any]] = []
    for directive_id, handler, directive in resolved:
        if directive.get("already_satisfied"):
            results.append(DirectiveResult(directive_id, already_satisfied=True, detail=None))
            landed.append(directive_id)
            continue

        ready, blocking = directive_gate_open(directive, jp_by_id, decisions)
        if not ready:
            blocked_jp_ids.update(blocking)
            continue

        try:
            # Outermost boundary (§ module docstring "SESSION IDENTITY
            # SHAPE"): a dispatch-table handler receives no explicit
            # session-id parameter -- it resolves identity, if it needs
            # one, from `os.environ` and may itself shell out (e.g. a
            # claim-mechanics handler's own git call). Mirror the active
            # `session_identity()` contextvar scope for the duration of
            # THIS ONE handler call only.
            with _mirror_session_env_for_subprocess():
                detail = handler(directive.get("args", []), repo_root)
        except Exception as exc:  # noqa: BLE001 - captured for the partial-mutation report
            if directive.get("advisory"):
                advisory_failures.append({"directive_id": directive_id, "error": str(exc)})
                continue
            partial_report: dict[str, Any] = {
                "error": str(exc),
                "failed_directive": directive_id,
                "landed": list(landed),
                "results": [r.to_report() for r in results],
            }
            if advisory_failures:
                partial_report["advisory_failures"] = advisory_failures
            if compensators:
                partial_report["compensation"] = _run_compensators(
                    compensators, results, directive_lookup, repo_root
                )
            return APPLY_EXIT_PARTIAL_MUTATION, partial_report
        results.append(DirectiveResult(directive_id, already_satisfied=False, detail=detail))
        landed.append(directive_id)

        # Mid-directive advisory (§ module docstring "COMPOSITION BUDGET
        # WIRING") -- WARN-ONLY, no control-flow effect, never for an
        # already_satisfied entry (it did no work this run). Runs right
        # after the directive that may have just been the slow one, so a
        # breach surfaces here rather than only at the run's final report.
        if composition_budget is not None:
            _budget_call(
                "mid-directive advisory",
                lambda directive_id=directive_id: composition_budget.record_invocation(
                    unit=directive_id
                ),
            )

            def _advise(directive_id: str = directive_id) -> None:
                if not composition_budget.advisory_check(unit=directive_id):
                    print(
                        "[composition-budget] mid-directive advisory breach after "
                        f"directive={directive_id!r}: composition="
                        f"{composition_budget.composition_id!r}",
                        file=sys.stderr,
                    )

            _budget_call("mid-directive advisory", _advise)

    # Composition-budget boundary #2 -- after the last mutation (§ module
    # docstring "COMPOSITION BUDGET WIRING"). Only reachable when the loop
    # above completed WITHOUT raising -- the PARTIAL_MUTATION `except`
    # branch returns before this point, so this check can never precede
    # (and therefore never triggers) `_run_compensators`. A breach here is
    # rc=0-with-loud-stderr, never PARTIAL_MUTATION: the mutation already
    # landed successfully, and PARTIAL_MUTATION's own reverse-compensation
    # pass exists to undo a run that failed mid-mutation, not one that
    # merely finished slowly.
    post_budget_breach: Optional[str] = None
    if composition_budget is not None:
        try:
            post_within_budget = _budget_call(
                "post-mutation boundary", lambda: composition_budget.check(unit="post_mutation")
            )
        except BudgetBreach as exc:
            post_budget_breach = str(exc)
        else:
            if post_within_budget is False:
                post_budget_breach = (
                    "composition budget breach: composition="
                    f"{composition_budget.composition_id!r} unit='post_mutation' "
                    "(skip-and-surface, observed after last mutation)"
                )
        if post_budget_breach is not None:
            print(f"[composition-budget] {post_budget_breach}", file=sys.stderr)

    if blocked_jp_ids:
        halted_report: dict[str, Any] = {
            "unresolved_judgment_points": sorted(blocked_jp_ids),
            "landed": landed,
            "results": [r.to_report() for r in results],
        }
        if advisory_failures:
            halted_report["advisory_failures"] = advisory_failures
        if post_budget_breach is not None:
            halted_report["budget_breach"] = post_budget_breach
        return APPLY_EXIT_HALTED_AT_JUDGMENT, halted_report

    ok_report: dict[str, Any] = {"landed": landed, "results": [r.to_report() for r in results]}
    if advisory_failures:
        ok_report["advisory_failures"] = advisory_failures
    if post_budget_breach is not None:
        ok_report["budget_breach"] = post_budget_breach
    return APPLY_EXIT_OK, ok_report


# ---------------------------------------------------------------------------
# Commit ledger join (C11, sweeping the sibling producers C5 left unwired --
# state/lessons/2026-08-18-a-ruling-applied-at-one-door-leaves-the-siblings-
# unswept-7c3e1f9a4d22.yaml). ONE shared write helper, consumed by every
# raw-`git commit` producer this module and its siblings own
# (`scoped_commit` below, `backlog_grind_assemble.apply._commit_one`, and
# `ops.ceremony.git_native.commit_authored_content`) rather than each
# reimplementing `commit_ledger.classify`'s kind/weight derivation a third
# time -- mirrors `ops.ceremony.scoped_git_commit._ledger_kind_and_weight`'s
# shape (same two reused primitives, `weight_for_path` +
# `review_brightline_gate.classify_surface`/`_is_noise_path`) without
# importing that module's private helper across a package boundary.
# ---------------------------------------------------------------------------

#: Mirrors `commit_ledger.oracle._DOCS_KIND` -- duplicated as a literal
#: (not imported) for the same reason `ops.ceremony.scoped_git_commit`
#: duplicates it: `oracle.py` declares the constant private to its own
#: two-figure split, and this module is a peer producer of the same
#: vocabulary, not a consumer of that constant.
_LEDGER_DOCS_KIND = "doctrine"

#: Mirrors `ops.ceremony.scoped_git_commit._LEDGER_CODE_KIND` -- the oracle
#: only ever branches on `kind == "doctrine"`, so one stable non-doctrine
#: label is sufficient here too.
_LEDGER_CODE_KIND = "code"


def _ledger_kind_and_weight(repo_root: str, paths: List[str]) -> "tuple[str, float]":
    """Commit-level `(kind, weight_basis)` for `paths`, identical shape to
    `ops.ceremony.scoped_git_commit._ledger_kind_and_weight` (see that
    function's own docstring) -- kept as a separate, small duplicate here
    rather than importing that module's private name across a package
    boundary."""
    # Local import: `commit_ledger.store` imports `ops.resolve_swept_baton`,
    # so a module-level import here closes a cycle back into this module and
    # leaves `commit_ledger.store` partially initialized whenever anything
    # imports it before `coordinator_core.ops` -- which de-registers this op.
    from coordinator_core.commit_ledger.classify import weight_for_path
    # Same reason, second edge: `coordinator_core.ops`'s package import is
    # EAGER, and `ops.session.boot_sweep` name-imports back out of this
    # module, so a module-level import of anything under `coordinator_core.
    # ops` here de-registers `session.boot_sweep` and
    # `session.sweep_consumed_handoffs` for any process that imports this
    # module before `coordinator_core.ops`. Measured 2026-08-19: 257 ops
    # instead of 259, at exit 0, with no traceback anywhere.
    from coordinator_core.ops.review_brightline_gate import (
        _is_noise_path,
        classify_surface,
    )

    total_weight = 0.0
    any_classified = False
    any_non_doctrine = False
    for p in paths:
        total_weight += weight_for_path(repo_root, p)
        if _is_noise_path(p):
            continue
        any_classified = True
        if classify_surface(p) != _LEDGER_DOCS_KIND:
            any_non_doctrine = True
    kind = _LEDGER_DOCS_KIND if (any_classified and not any_non_doctrine) else _LEDGER_CODE_KIND
    return kind, total_weight


def _committer_id_for_ledger(repo_root: Path) -> str:
    """The identity to bill a just-landed commit's ledger entry to:
    prefers the active `session_identity()` contextvar scope (the shape
    every caller of `scoped_commit`/`_commit_one` already runs inside),
    falling back to `session_core.resolve_session_id` -- the same ambient
    env-var read `ops.ceremony.scoped_git_commit._resolve_committing_
    session_id` uses -- for a caller with no active scope."""
    for var in SESSION_ENV_VARS:
        val = current_session_env().get(var)
        if val:
            return val
    return session_core.resolve_session_id(str(repo_root))


def record_ledger_entry(
    repo_root: Path,
    paths: List[str],
    sha: Optional[str],
    *,
    committer_id_override: Optional[str] = None,
) -> None:
    """Append `sha`'s commit-ledger entry (C1/C5) for a producer that
    committed OUTSIDE `ceremony.scoped_git_commit` -- this module's own
    `scoped_commit`, `backlog_grind_assemble.apply._commit_one`, and
    `ops.ceremony.git_native.commit_authored_content` all call this as the
    LAST step after their commit has already landed (C11: sweeping the
    sibling producers C5 left unwired, see this section's own module
    comment).

    `committer_id_override` -- for a caller that already holds a resolved
    identity of its own (e.g. `commit_authored_content`'s own
    `attributed_session_id`) rather than the active `session_identity()`
    contextvar scope -- passed straight through instead of re-deriving the
    committer a second, independent way (the same disagreeing-copies hazard
    `commit_authored_content`'s own docstring names for its trailer
    resolution). `None` (the default) falls back to
    `_committer_id_for_ledger`.

    Hard constraint, mirrored from C5's own: a ledger write must never
    fail the commit it accompanies -- `sha` is `None` (a clean no-op commit)
    is a silent return, and every other failure mode (classification, owner
    resolution, the append itself) is swallowed under one broad `except
    Exception`, logged, never raised.
    """
    # Local import: `commit_ledger.store` imports `ops.resolve_swept_baton`,
    # so a module-level import here closes a cycle back into this module and
    # leaves `commit_ledger.store` partially initialized whenever anything
    # imports it before `coordinator_core.ops` -- which de-registers this op.
    from coordinator_core.commit_ledger.resolve_owner import resolve_owner_handoff_id
    from coordinator_core.commit_ledger.store import append_entry as _ledger_append_entry

    if not sha:
        return
    try:
        kind, weight_basis = _ledger_kind_and_weight(str(repo_root), paths)
        committer_id = committer_id_override or _committer_id_for_ledger(repo_root)
        handoff_id, _degraded = resolve_owner_handoff_id(committer_id, Path(repo_root))
        if handoff_id:
            _ledger_append_entry(
                handoff_id,
                sha,
                kind,
                weight_basis=weight_basis,
                cwd=str(repo_root),
            )
        # `handoff_id is None` is the legitimate standalone outcome
        # (`resolve_owner_handoff_id`'s own zero-held-claims arm) -- not an
        # error, nothing to bill this commit to, no warning.
    except Exception:
        _LOG.warning(
            "contract.apply_base: commit ledger write failed for %s; the "
            "commit already landed and is unaffected",
            sha,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Scoped commit — the ONE commit shape every consumer's `apply` makes,
# pathspec-limited to the artifact it itself just mutated. `apply` runs
# against a shared concurrent-EM working tree where a sibling session's
# own edits may already sit staged in the same index — `git add -A`/
# `git commit` with no pathspec would sweep those peer files into this
# run's commit. Every git call below instead names the one resolved
# artifact path explicitly, both on `add` and on `commit`.
# ---------------------------------------------------------------------------


def scoped_commit(
    repo_root: Path,
    artifact_rel_path: str,
    message: str,
    run_git: Callable[[list[str], Path], Any],
) -> Optional[str]:
    """Stages then commits ONLY `artifact_rel_path`, via an explicit
    pathspec on both the `add` and the `commit`. `run_git` is the
    caller's own `(args, cwd) -> CompletedProcess`-shaped git runner —
    this module has no subprocess opinion of its own, so a consumer with
    an in-process read-model fast path (e.g. pickup_assemble's own
    `_run_git`) plugs in unchanged. Returns the new commit's SHA, or
    `None` when there was nothing to commit for this path (a clean run
    whose directives were all `already_satisfied`, or an artifact path
    this run never actually wrote to) — a no-op, not a failure.

    Never resolves `run_git`'s `cwd` from anything but the caller-
    supplied `repo_root`, and never widens the pathspec beyond the one
    resolved path — there is no seam here through which a second path
    could be added to this commit.
    """
    if not artifact_rel_path:
        return None
    resolved = assert_in_repo_root(Path(artifact_rel_path), repo_root)
    pathspec = ["--", str(resolved)]

    with _mirror_session_env_for_subprocess():
        add_proc = run_with_lock_retry(lambda: run_git(["add", *pathspec], repo_root))
    if add_proc.returncode != 0:
        raise RuntimeError(
            f"git add {artifact_rel_path} failed (rc={add_proc.returncode}, "
            f"cwd={repo_root}): {add_proc.stderr.strip() or '<no stderr>'}"
        )

    with _mirror_session_env_for_subprocess():
        unchanged = run_git(["diff", "--cached", "--quiet", *pathspec], repo_root)
    if unchanged.returncode == 0:
        return None

    with _mirror_session_env_for_subprocess():
        commit_proc = run_with_lock_retry(
            lambda: run_git(["commit", "-m", message, *pathspec], repo_root)
        )
    if commit_proc.returncode != 0:
        raise RuntimeError(
            f"git commit {artifact_rel_path} failed (rc={commit_proc.returncode}, "
            f"cwd={repo_root}): {commit_proc.stderr.strip() or '<no stderr>'}"
        )

    with _mirror_session_env_for_subprocess():
        sha_proc = run_git(["rev-parse", "HEAD"], repo_root)
    landed_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else None
    record_ledger_entry(repo_root, [artifact_rel_path], landed_sha)
    return landed_sha
