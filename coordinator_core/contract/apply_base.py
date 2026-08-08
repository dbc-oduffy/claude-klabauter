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

Contract: example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md, chunk C2

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
    - Do NOT build a general transaction manager, two-phase commit, or
      journal here — `compensators` (2026-07-29, `baton_assemble`'s
      orphaned-scaffold finding) is a single, narrow reaction to
      `APPLY_EXIT_PARTIAL_MUTATION` only: run each landed directive's own
      registered undo, in reverse landing order, never a generalized
      rollback engine. See `execute_directives`'s own docstring for the
      full contract (opt-in, additive, byte-identical when omitted).
"""
from __future__ import annotations

import dataclasses
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from coordinator_core.git_lock_retry import run_with_lock_retry

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


# ---------------------------------------------------------------------------
# Session-id propagation — explicit only, never an ambient tier-4
# sentinel. `SESSION_ENV_VARS` is what a resolved explicit id gets
# written INTO for the duration of a call (both identity chains a
# consumer's composed primitives may read); `SESSION_ENV_READ_ORDER` is
# what an implicit id is read FROM, highest-precedence first, when no
# explicit id is supplied.
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


@contextmanager
def session_identity(
    session_id: str, *, env_vars: tuple[str, ...] = SESSION_ENV_VARS
) -> Iterator[None]:
    previous = {var: os.environ.get(var) for var in env_vars}
    for var in env_vars:
        os.environ[var] = session_id
    try:
        yield
    finally:
        for var, value in previous.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


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
    additive to, never substitutes for."""
    outcomes: list[dict[str, Any]] = []
    for result in reversed(results):
        if result.already_satisfied:
            continue
        compensator = compensators.get(result.directive_id)
        if compensator is None:
            continue
        outcome: dict[str, Any] = {"directive_id": result.directive_id, "attempted": True}
        try:
            compensator(directive_lookup[result.directive_id], repo_root, result.detail)
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
        claim_grant = resolve_claim_grant()
        if claim_grant.get("verdict") not in GRANTED_VERDICTS:
            return APPLY_EXIT_CLAIM_DENIED, {
                "claim_grant": claim_grant,
                "landed": [],
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
        resolved = [(d["id"], resolve_cli(dispatch_table, d["cli"]), d) for d in ordered]
    except UnrecognizedDirective as exc:
        return APPLY_EXIT_TRANSPORT_FAIL, {"error": str(exc), "landed": []}

    directive_lookup = {directive_id: d for directive_id, _, d in resolved}
    landed: list[str] = []
    results: list[DirectiveResult] = []
    blocked_jp_ids: set[str] = set()
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
            detail = handler(directive.get("args", []), repo_root)
        except Exception as exc:  # noqa: BLE001 - captured for the partial-mutation report
            partial_report: dict[str, Any] = {
                "error": str(exc),
                "failed_directive": directive_id,
                "landed": list(landed),
                "results": [r.to_report() for r in results],
            }
            if compensators:
                partial_report["compensation"] = _run_compensators(
                    compensators, results, directive_lookup, repo_root
                )
            return APPLY_EXIT_PARTIAL_MUTATION, partial_report
        results.append(DirectiveResult(directive_id, already_satisfied=False, detail=detail))
        landed.append(directive_id)

    if blocked_jp_ids:
        return APPLY_EXIT_HALTED_AT_JUDGMENT, {
            "unresolved_judgment_points": sorted(blocked_jp_ids),
            "landed": landed,
            "results": [r.to_report() for r in results],
        }

    return APPLY_EXIT_OK, {"landed": landed, "results": [r.to_report() for r in results]}


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

    add_proc = run_with_lock_retry(lambda: run_git(["add", *pathspec], repo_root))
    if add_proc.returncode != 0:
        raise RuntimeError(
            f"git add {artifact_rel_path} failed (rc={add_proc.returncode}, "
            f"cwd={repo_root}): {add_proc.stderr.strip() or '<no stderr>'}"
        )

    unchanged = run_git(["diff", "--cached", "--quiet", *pathspec], repo_root)
    if unchanged.returncode == 0:
        return None

    commit_proc = run_with_lock_retry(
        lambda: run_git(["commit", "-m", message, *pathspec], repo_root)
    )
    if commit_proc.returncode != 0:
        raise RuntimeError(
            f"git commit {artifact_rel_path} failed (rc={commit_proc.returncode}, "
            f"cwd={repo_root}): {commit_proc.stderr.strip() or '<no stderr>'}"
        )

    sha_proc = run_git(["rev-parse", "HEAD"], repo_root)
    return sha_proc.stdout.strip() if sha_proc.returncode == 0 else None
