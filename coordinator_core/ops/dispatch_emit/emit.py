"""
coordinator_core.ops.dispatch_emit.emit — waves + commit phases -> one
conformant Workflow ``.mjs`` script.

Purpose: the single script-composition entry point for the dispatch-emit
pipeline (docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md
§ C4). Consumes ``wave_map.build_waves``'s ordered waves and
``pathspec.commit_pathspec``/``pathspec.terminal_test_scope``'s derived
pathspecs/scope, and composes ONE Workflow script whose ``meta.phases``
alternate executor waves and ``coordinator:git-commit-agent`` commit phases,
ending in one terminal ``coordinator:test-runner`` phase.

## Reuse boundary (staff-eng review, ratified by EM 2026-08-13)

``workflow_scaffold._compose_script`` is NOT wrapped or extended here. It
hardcodes a literal ``TODO: prompt for {title}`` body, a fixed
``label: 'work:<title>'``, and emits NO ``agentType`` at any call site — this
module needs per-phase ``agentType`` (``coordinator:executor`` for wave rows,
``coordinator:git-commit-agent`` for commit phases, ``coordinator:test-runner``
for the terminal phase) and real prompt bodies, neither of which
``_compose_script`` has a parameter or extension point for. Wrapping it would
mean string-surgery on its generated JS output — strictly worse than
composing fresh. This module reuses exactly one shared primitive,
``workflow_scaffold._js_string_literal`` (the JS-escaping helper), and
composes everything else itself. Neither ``_compose_script`` nor
``_normalize_phases`` is edited — both back ``workflow.scaffold``, a
registered op with its own round-trip test and DoE veneer.

``_normalize_phases`` is not called here at all: on an empty/omitted
``phases`` list it silently substitutes a single default ``{"title": "Run",
"detail": "primary work"}`` phase, which is correct for a caller-driven
scaffolder and directly hostile to AC4/AC10 here, which require a fail-loud
refusal on an empty wave list. ``emit_script`` refuses (``NoWavesError``)
on an empty wave list BEFORE any phase/script composition begins.

## Commit-claimability preflight (AC14)

``pathspec.commit_pathspec``/``pathspec.py``'s own module docstring (§ The
executed premise this module's output inherits) document a residual this
module closes: a provenance-clean, AC3/AC4-passing pathspec is not
automatically claimable at runtime by the dispatched ``coordinator:git-
commit-agent`` — verified 2026-08-12, a dispatched committer refused a path
the EM's own ``scoped-git-commit`` accepted for the identical pathspec at
``01e183d584c5``.

That refusal is NOT decidable at emit time by inspecting the pathspec
itself. The denying condition lives in ``coordinator_core.session.
claim_index`` (see that module's docstring, "POSITIVE/NEGATIVE ASYMMETRY"
and ``UNANSWERABLE``) and ``ceremony.scoped_git_commit``'s
``include_orphans`` docstring (~line 856): claimant liveness and the
UNANSWERABLE-path branch are both properties of the LIVE session-claim
state at the moment the commit call runs — a full reverse-index rebuild
over every session directory present on disk right then — not a static
property of the path string. The emitted script and the dispatched
committer run in different, later-spawned sessions than the emitter, so
emit time has no session to probe.

The fix therefore cannot live in ``pathspec.py`` as a static refusal (there
is nothing about a path alone to check). Instead ``compose_script`` emits
ONE extra phase, FIRST in ``meta.phases``, before any executor wave: a
``coordinator:git-commit-agent`` call whose prompt asks it to verify
claimability of the union of every commit phase's pathspec WITHOUT staging
or committing anything, and to refuse (BLOCKED) immediately if any path
would be denied. This is the identical runtime check every commit phase
already performs later, run once up front against the same agentType, so a
would-be-refused pathspec fails BEFORE any wave's work exists rather than
after. It is not a new mechanism and it does not widen or manufacture a
claim — see the plan's anti-scope on minting claims to force a commit to
succeed, which this preflight does not do: a refusal here is a refusal,
full stop, identical in shape to what a real commit phase would do.

**What this preflight does NOT promise.** It narrows the window; it does
not close it. Claim state is live session state, mutable while the script
runs — a peer session can claim, release, or die against one of these paths
between the preflight and the commit phase that needs it, and the commit
phase will then refuse exactly as it would have without a preflight. That
is inherent to checking mutable state ahead of use and is not a defect to
"fix" by caching the preflight's verdict and having later phases trust it:
a cached claim verdict is a stale claim verdict, and trusting one is how a
commit phase would proceed against a path a live peer now holds. The
preflight's value is that the overwhelmingly common failure — a pathspec
that was never claimable by a dispatched committer at all — is caught
before any wave's work exists, instead of after.

## Ordering (AC9)

The terminal ``coordinator:test-runner`` phase is placed AFTER the final
commit phase, as the LAST entry in ``meta.phases`` — it reports; it does not
gate (see plan § Terminal test phase folded in). Every wave in ``waves`` gets
exactly one executor phase immediately followed by exactly one commit phase,
so the entry immediately before the terminal test phase is always a commit
phase.

## model: 'sonnet' (AC11)

Every emitted ``agent()`` call — executor wave, commit phase, and the
terminal test phase alike — carries an ACTIVE ``model: 'sonnet'`` in its
opts object, never a commented placeholder and never a model-less call left
to inherit the session model. This is a settled PM ruling
(docs/wiki/workflow-skeleton-stamper.md § Scaffold defaults model best
practice by construction) enforced at WARN tier only in
``_workflow_contract.run_checks`` — AC5's zero-ERROR bar does NOT catch an
omission here, so this module enforces it structurally: every call-composing
helper below hardcodes the ``model: 'sonnet'`` opts entry inline, with no
parameter or code path that could omit it.

## Tier-T only (Anti-scope)

The terminal phase's ``agentType`` is always ``coordinator:test-runner`` —
the emitter never reaches for any other agent or tier here. Tier F and Tier
U both require a live session-scoped test-invocation grant that no emitted
phase (running with nobody present) can obtain; ``coordinator:test-runner``
is Tier-T-only by its own agent description, which is what makes this phase
safe to emit unconditionally.

## Permission-mode (contract confirmation, 2026-08-13)

No ``mode:``/permission-mode key is ever placed on an emitted ``agent()``
call — DoE's live-tool capture found no permission-mode carrier on the
``Workflow`` agent-call path at all (options: ``label``, ``phase``,
``schema``, ``model``, ``effort``, ``isolation``, ``agentType``, nothing
else). This module has no code path that emits one.

## Vehicle: EM-dispatched Agent, not a fired-and-forgotten Workflow (live upstream defect)

DoE-claude's ``skills/execute-plan/SKILL.md`` § Vehicle default QUALIFIES
states that a Workflow ``agent()`` spawn is not an ``Agent`` tool call, so
injected ``contract_blocks`` never arrive on that path, and that 33 of 34
coordinator-typed agents carry a ``contract_blocks`` row — so a plan wave of
coordinator-typed agents belongs on the ``Agent`` path today, not fired
unattended as a Workflow script. Verified OPEN at DoE-claude HEAD
(2026-08-14). Until that seam closes, the script this module emits is a
durable machine-derived wave-map artifact an EM dispatches FROM via
``Agent``, one phase at a time — not a script to run unattended. A future
reader whose check shows the seam closed should DELETE this note rather than
cement it, per the same qualifier in the upstream doctrine text.

Negative-spec:
  - Does NOT derive waves, pathspecs, or the terminal test scope — those are
    ``wave_map.py``/``pathspec.py`` (C2/C3). This module composes script
    text from their already-derived output only.
  - Does NOT edit or call into ``_compose_script``/``_normalize_phases`` —
    see § Reuse boundary above.
  - Does NOT write to disk — returns script text only; C5 registers the
    disk-writing op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.pathspec import commit_pathspec, terminal_test_scope
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, read_spine
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, _normalize_path, build_waves
from coordinator_core.ops.workflow_scaffold import _js_string_literal
from coordinator_core.write_guards.block_subagent_plan_body_write import _PLAN_BODY_RE

_MODEL_OPT = "model: 'sonnet'"
_EXECUTOR_AGENT_TYPE = "coordinator:executor"
_ENRICHER_AGENT_TYPE = "coordinator:enricher"
_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"
_TEST_AGENT_TYPE = "coordinator:test-runner"


class MixedAgentTypeRowError(ValueError):
    """Raised when one row's declared ``writes`` span both an immutable
    body path (``docs/plans/*.md`` or ``docs/problems/*.md``) and an
    ordinary code/doc path (Defect A).

    Neither ``coordinator:executor`` nor ``coordinator:enricher`` can satisfy
    both halves of a mixed row: ``write_guards.block_subagent_plan_body_write``
    hard-denies ``coordinator:executor`` writing a plan body or a ratified
    problem-set, and routing the whole row to ``coordinator:enricher`` would
    silently ask an enricher to edit ordinary code it has no charter for.
    This module's posture is fail-loud refusal over a fabricated dispatch
    (see ``NoWavesError`` and ``pathspec.NoWritesDeclaredError``) — a mixed
    row means the spine itself needs splitting into an immutable-body chunk
    and a code chunk, not a guess here.
    """


class NoWavesError(ValueError):
    """Raised when the spine derives zero waves (empty spine or empty rows).

    Refusing here, BEFORE any phase/script composition, is what stands in
    for ``workflow_scaffold._normalize_phases``'s caller-driven default-phase
    fallback — that fallback is correct for a caller-driven scaffolder and
    directly hostile to AC4/AC10 here, which require fail-loud refusal
    rather than a fabricated phase.
    """


def _dedupe_preserve_order(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def _is_immutable_body_path(path: str) -> bool:
    """True if ``path`` names a ``docs/plans/*.md`` or ``docs/problems/*.md``
    immutable body file — the exact denial surface of
    ``write_guards.block_subagent_plan_body_write``'s ``_PLAN_BODY_RE``.

    Imports and matches that guard's own regex directly rather than
    re-deriving the two prefixes here: the guard is the authority on what
    it denies, and a local re-derivation would silently drift the next time
    that surface widens (as it already has once, 2026-07-24, to add
    ``docs/problems/**``). Matched against the normalized POSIX form
    (``wave_map._normalize_path``) so case and ``./``/``..`` variance in a
    declared path can't dodge the check — the same normalization
    ``_writes_overlap`` already trusts for write-set collision.
    """
    normalized = _normalize_path(path)
    return bool(_PLAN_BODY_RE.search(str(normalized)))


def _row_agent_type(row: WaveRow) -> str:
    """Derive a wave row's ``agentType`` from its declared write targets.

    A row writing ANY ``docs/plans/*.md`` or ``docs/problems/*.md`` path
    routes to ``coordinator:enricher`` — ``coordinator:executor`` is
    hard-denied from editing either surface by
    ``write_guards.block_subagent_plan_body_write`` (Defect A, widened
    2026-07-24 to also cover ``docs/problems/**``). Every other row stays
    ``coordinator:executor``. A row whose writes span both kinds raises
    ``MixedAgentTypeRowError`` — see its docstring.

    ``UNDECLARED`` writes derive to ``coordinator:executor``: an UNDECLARED
    row is refused downstream by ``pathspec.commit_pathspec`` (raising
    ``NoWritesDeclaredError``) before this module ever composes a call for
    it, so no dispatch is ever emitted with this fallback live — the choice
    only has to be a safe placeholder, not a real routing decision.
    """
    if row.writes is UNDECLARED:
        return _EXECUTOR_AGENT_TYPE

    immutable_body = any(_is_immutable_body_path(p) for p in row.writes)
    other = any(not _is_immutable_body_path(p) for p in row.writes)

    if immutable_body and other:
        raise MixedAgentTypeRowError(
            f"row {row.id!r} declares writes spanning both an immutable "
            "docs/plans/*.md or docs/problems/*.md body and an ordinary "
            "path — split the row instead of guessing an agentType"
        )
    if immutable_body:
        return _ENRICHER_AGENT_TYPE
    return _EXECUTOR_AGENT_TYPE


def _wave_phase_title(index: int, wave: list[WaveRow]) -> str:
    ids = ", ".join(row.id for row in wave)
    return f"Wave {index + 1}: {ids}"


def _commit_phase_title(index: int) -> str:
    return f"Commit wave {index + 1}"


_TEST_PHASE_TITLE = "Scoped test run"
_PREFLIGHT_PHASE_TITLE = "Preflight: commit claimability"


def _row_prompt(row: WaveRow) -> str:
    return f"Execute {row.id}: {row.title}"


def _wave_agent_calls(wave: list[WaveRow], phase_title: str) -> str:
    """Compose the ``phase()`` + agent-dispatch call(s) for one executor wave.

    A single-row wave emits one ``await agent(...)`` call (a serial gate,
    per the workflow-emitter-contract topology). A multi-row wave emits one
    ``await parallel(...)`` call wrapping one ``agent()`` per row (the
    parallel-wave shape).
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"

    if len(wave) == 1:
        row = wave[0]
        call = (
            "  await agent("
            f"{_js_string_literal(_row_prompt(row))}, "
            "{ "
            f"label: {_js_string_literal(f'work:{row.id}')}, "
            f"phase: {_js_string_literal(phase_title)}, "
            f"agentType: {_js_string_literal(_row_agent_type(row))}, "
            f"{_MODEL_OPT} "
            "});"
        )
        return f"{phase_call}\n{call}"

    item_calls = ",\n".join(
        "    () => agent("
        f"{_js_string_literal(_row_prompt(row))}, "
        "{ "
        f"label: {_js_string_literal(f'work:{row.id}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_row_agent_type(row))}, "
        f"{_MODEL_OPT} "
        "})"
        for row in wave
    )
    call = (
        "  await parallel([\n"
        f"{item_calls}\n"
        "  ]);"
    )
    return f"{phase_call}\n{call}"


def _commit_agent_call(pathspec: list[str], phase_title: str, index: int) -> str:
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    prompt = f"Commit wave {index + 1}'s work. Pathspec: [{', '.join(pathspec)}]."
    call = (
        "  await agent("
        f"{_js_string_literal(prompt)}, "
        "{ "
        f"label: {_js_string_literal(f'commit:wave-{index + 1}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_COMMIT_AGENT_TYPE)}, "
        f"{_MODEL_OPT} "
        "});"
    )
    return f"{phase_call}\n{call}"


def _preflight_agent_call(pathspec: list[str], phase_title: str) -> str:
    """Compose the preflight phase's ``phase()`` + ``agent()`` call (AC14).

    Dispatches the SAME ``agentType`` every commit phase later uses
    (``coordinator:git-commit-agent``) against the UNION of every commit
    phase's pathspec, asked to verify claimability only -- no staging, no
    commit. See module docstring § Commit-claimability preflight.
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    prompt = (
        "Preflight only -- do not stage or commit anything. Verify that "
        f"every path in [{', '.join(pathspec)}] is currently claimable and "
        "committable by you. If any path would be refused, report BLOCKED "
        "immediately, naming the refused paths and the denial reason, "
        "before any further phase runs."
    )
    call = (
        "  await agent("
        f"{_js_string_literal(prompt)}, "
        "{ "
        f"label: {_js_string_literal('preflight:commit-claimability')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_COMMIT_AGENT_TYPE)}, "
        f"{_MODEL_OPT} "
        "});"
    )
    return f"{phase_call}\n{call}"


def _test_agent_call(scope: list[str], phase_title: str) -> str:
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    prompt = f"Run the scoped test targets: [{', '.join(scope)}]. Report raw evidence; do not gate."
    call = (
        "  await agent("
        f"{_js_string_literal(prompt)}, "
        "{ "
        f"label: {_js_string_literal('test:terminal')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_TEST_AGENT_TYPE)}, "
        f"{_MODEL_OPT} "
        "});"
    )
    return f"{phase_call}\n{call}"


def _meta_block(name: str, description: str, phase_titles: list[str]) -> str:
    phases_literal = ", ".join(_js_string_literal(t) for t in phase_titles)
    return (
        "export const meta = {\n"
        f"  name: {_js_string_literal(name)},\n"
        f"  description: {_js_string_literal(description)},\n"
        f"  phases: [{phases_literal}],\n"
        "};\n"
    )


def compose_script(
    waves: list[list[WaveRow]],
    *,
    name: str,
    description: str,
    repo_root: Optional[Path] = None,
) -> str:
    """Compose one Workflow ``.mjs`` script text from already-derived ``waves``.

    Refuses (``NoWavesError``) if ``waves`` is empty — see module docstring
    § Reuse boundary. Every other refusal (``NoWritesDeclaredError``,
    ``NoTestTargetError``) is raised by ``pathspec.commit_pathspec``/
    ``pathspec.terminal_test_scope`` and propagates unchanged: this function
    adds no derivation of its own.
    """
    if not waves:
        raise NoWavesError(
            "spine derives zero waves — refusing to emit an empty script "
            "(no fabricated default phase; see _normalize_phases reuse "
            "boundary in the module docstring)"
        )

    # AC14: derive every wave's commit pathspec up front (same call, same
    # per-wave order/refusal behaviour as before) so the preflight phase can
    # be composed from their union BEFORE the first wave/commit phase block
    # is built -- see module docstring § Commit-claimability preflight.
    wave_pathspecs = [commit_pathspec(wave) for wave in waves]
    preflight_pathspec = _dedupe_preserve_order(
        path for pathspec in wave_pathspecs for path in pathspec
    )

    body_blocks: list[str] = []
    phase_titles: list[str] = []

    phase_titles.append(_PREFLIGHT_PHASE_TITLE)
    body_blocks.append(_preflight_agent_call(preflight_pathspec, _PREFLIGHT_PHASE_TITLE))

    for index, wave in enumerate(waves):
        wave_title = _wave_phase_title(index, wave)
        phase_titles.append(wave_title)
        body_blocks.append(_wave_agent_calls(wave, wave_title))

        pathspec = wave_pathspecs[index]
        commit_title = _commit_phase_title(index)
        phase_titles.append(commit_title)
        body_blocks.append(_commit_agent_call(pathspec, commit_title, index))

    scope = terminal_test_scope(waves, repo_root=repo_root)
    phase_titles.append(_TEST_PHASE_TITLE)
    body_blocks.append(_test_agent_call(scope, _TEST_PHASE_TITLE))

    meta_block = _meta_block(name, description, phase_titles)
    body = "\n\n".join(body_blocks)

    return (
        f"{meta_block}\n"
        "async function run(ctx) {\n"
        f"{body}\n"
        "}\n"
    )


def emit_script(
    plan_path,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> str:
    """Read ``plan_path``'s task spine and compose one Workflow script text.

    Composes the full pipeline: ``spine_read.read_spine`` ->
    ``wave_map.build_waves`` -> ``compose_script``. ``name``/``description``
    default to the plan file's stem and a fixed generic description when
    omitted — this module reads no plan frontmatter (out of scope; see the
    C4 reads list).
    """
    plan_path = Path(plan_path)
    rows = read_spine(plan_path)
    waves = build_waves(rows)

    resolved_name = name or plan_path.stem
    resolved_description = description or (
        f"Emitted executor/commit/test workflow for {plan_path.stem}"
    )

    return compose_script(
        waves,
        name=resolved_name,
        description=resolved_description,
        repo_root=repo_root,
    )


def assert_zero_errors(script: str) -> None:
    """Feed ``script`` into ``_workflow_contract.run_checks`` and raise if any
    ERROR-severity finding is present (AC5). WARN findings never raise —
    this mirrors ``workflow.scaffold``'s own round-trip discipline."""
    findings = run_checks(script)
    errors = [f for f in findings if f.severity is Severity.ERROR]
    if errors:
        details = "; ".join(f"{f.code}: {f.message}" for f in errors)
        raise ValueError(f"emitted script failed run_checks with ERROR findings: {details}")
