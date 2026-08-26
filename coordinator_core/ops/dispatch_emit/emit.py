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

## Top-level body, never a defined-but-uninvoked wrapper (BREAK-CLASS FIX)

``compose_script`` emits every ``phase()``/``agent()``/``parallel()`` call as
a TOP-LEVEL statement in the ``.mjs`` module body, never inside a
``function run(ctx) { ... }`` block that nothing calls. Measured, not
inferred: a minimal probe using exactly that wrapper shape (``wf_abfe2580-
fb2``) returned the top-level value ``{"wrapperInvoked": false}`` with
``agent_count: 0`` — the harness Workflow contract executes the script BODY
directly (``export const meta = {...}`` then top-level statements); it never
looks for, defines, or calls a ``run`` export. A defined-but-uninvoked
``run()`` therefore spawns nothing and reports success having done nothing.
Top-level ``await`` is valid ESM syntax, which this module's ``.mjs`` output
already is (see ``export const meta`` above it). ``workflow_scaffold.
_compose_script`` carries the identical defect and is NOT fixed here — out
of this module's write scope; tracked at
``state/bug-backlog/2026-08-18-workflow-scaffold-emits-an-inert-script-*.yaml``.

## Review phases (a)

Pre-flight checkers, the integrator, and named reviewers compose onto the
SAME phase mechanism as an executor wave (``phase()`` + ``agent()``/
``parallel()``) — proven unnecessary to duplicate by the spike's review-
phase probe (docs/research/spike-verdicts/2026-08-18-claude-klabauter-fires-an-
emitted-workflow.md, probe 3: a review-phase ``agentType`` runs through the
identical mechanism unchanged). No parallel review-runner exists here.

The division of labour is load-bearing. ``coordinator/routing.md``
(DoE-claude) cannot supply reviewer selection directly here — it keys
Reviewers on change scope (hotfix / 2-5 files / architectural / test-only /
doc-only) resolved by domain signal-gating, which this emitter cannot
evaluate, not on a sizing object's t-shirt. Minting a sizing->reviewer
mapping in THIS module would author doctrine in a plane CLAUDE.md says
Claude-klabauter does not own; runtime-parsing ``routing.md`` prose would add a
cross-plane read dependency against ``docs/reference/boundary-and-data-
planes.md``. Both excluded.

The concern splits instead: ``derive_review_tier`` reads the plan's own
frontmatter ``sizing_object:`` citation and that sizing-object's
``estimate.tshirt`` — data this repo owns, writes, and validates against
its own schema (``sizing-object.schema.json``) — and maps it through
``_TSHIRT_TO_REVIEW_TIER`` onto ONE of three tiers, ``lightweight`` /
``standard`` / ``full``. That three-rung vocabulary is not invented here —
it is the same vocabulary ``coordinator:staff-session`` already selects a
tier on, so a fragment author has a live cross-repo precedent rather than a
fresh one. The tier->reviewer roster stays entirely DoE-owned, supplied as
a machine-readable fragment (shape documented at ``review_mint.roster.
parse_stages``) that this module consumes and never authors. Composing a
review phase therefore needs BOTH a derived tier (this repo's data) AND a
supplied fragment (DoE's data) — either alone composes nothing.

Fragment parsing and stage composition are NOT reimplemented here.
``review_mint.roster.parse_stages`` (C1) turns the caller-supplied fragment
dict into an ordered ``[Stage, ...]`` list — a flat ``schema_version 1``
list reads as one non-gated stage, a staged (``schema_version`` of 2 or
    more)
fragment reads as its own ordered stages, gate flag and all — and
``review_mint.compose.compose`` (C2) turns that stage list into
``(phase_title, script_block)`` pairs this module splices straight into its
own ``phase_titles``/``body_blocks``, the same shape every other section of
this module already produces. This module supplies only what is its own to
supply: the injected fragment dict (this caller does NOT call
``review_mint.op.load_fragment()`` — that cross-repo pointer resolution
lives at the op boundary only, see that module's docstring), the
post-execution review prompt (``_REVIEW_PROMPT``), and a ``gate_policy``
(``_review_gate_policy``) that disarms every gate's early-return so a gate
verdict here narrates rather than suppresses the terminal test phase that
always runs after — this caller's commits have already landed by the time
a review phase runs, so an abort here must never skip the test phase that
reports on those landed commits. ``review.mint_workflow`` (pre-execution)
supplies its own, different prompt and a real abort ``gate_policy`` — the
two callers never share either.

## Commit-phase placement keyed to wave size (b)

The commit-phase MECHANISM (``_commit_agent_call``) is unchanged — only
WHERE it fires is new. A wave at or under ``_WAVE_COMMIT_BATCH_THRESHOLD``
(10) rows keeps firing exactly one commit phase after it, identical to
every wave before this chunk. A wave OVER that many rows is split into
consecutive batches of at most 10 rows apiece (``_split_wave_for_commit_
placement``), each batch getting its own executor phase immediately
followed by its own commit phase against that batch's own (recomputed,
narrower) pathspec — so a >10-row wave commits incrementally rather than
holding every row's work uncommitted until one single trailing commit
covers all of it.

## Ordering (AC9)

The terminal ``coordinator:test-runner`` phase is placed AFTER the final
commit phase, as the LAST entry in ``meta.phases`` — it reports; it does not
gate (see plan § Terminal test phase folded in). Every wave in ``waves`` gets
exactly one executor phase immediately followed by exactly one commit phase,
so the entry immediately before the terminal test phase is always a commit
phase.

## An ACTIVE model: on every call, tiered by agentType (AC11)

Every emitted ``agent()`` call — executor wave, commit phase, and the
terminal test phase alike — carries an ACTIVE ``model:`` in its opts object,
never a commented placeholder and never a model-less call left to inherit
the session model. This is a settled PM ruling
(docs/wiki/workflow-skeleton-stamper.md § Scaffold defaults model best
practice by construction) enforced at WARN tier only in
``_workflow_contract.run_checks`` — AC5's zero-ERROR bar does NOT catch an
omission here, so this module enforces it structurally: every call-composing
helper below routes through ``_model_opt``, with no parameter or code path
that could omit it.

Which model is a per-``agentType`` decision, not one constant. A call-site
``model:`` OVERRIDES the agent definition's own frontmatter, so a blanket
``'sonnet'`` silently outranked ``git-commit-agent``'s and
``test-runner``'s charter tier and billed a Sonnet for mechanical work.
``_AGENT_MODELS`` mirrors the charter tier each definition declares
(DoE-claude ``coordinator/agent-effort-registry.yaml``); keep the two in
step when either moves.

## Tier-T only (Anti-scope)

The terminal phase's ``agentType`` is always ``coordinator:test-runner`` —
the emitter never reaches for any other agent or tier here. Tier F and Tier
U both require a live session-scoped test-invocation grant that no emitted
phase (running with nobody present) can obtain; ``coordinator:test-runner``
is Tier-T-only by its own agent description, which is what makes this phase
safe to emit without a live grant.

That safety is about the AGENT TIER, not about the phase being mandatory.
The phase is composed only when ``pathspec.terminal_test_scope`` yields at
least one target; a spine writing nothing testable gets a ``log()`` line
declaring the omission instead (``_no_test_scope_narration``). Composing it
unconditionally is what made a prose deliverable unrepresentable — the
scope derivation had to refuse in order to defend an invariant this module
imposed, and the refusal surfaced to plan authors as an unsatisfiable
guard. See ``pathspec``'s module docstring § The sharp edge AC16 exists for.

## A review-phase gate abort never suppresses the terminal test phase (Anti-scope)

This caller's commits have already landed by the time any review phase it
composes runs (post-execution). ``_review_gate_policy`` therefore disarms
every gated stage's early-return unconditionally (returns ``""`` always) —
a gate verdict here narrates onto the emitted script, it never composes a
``return`` that would skip the terminal ``coordinator:test-runner`` phase
``compose_script`` appends after the review phase whenever the derived
scope is non-empty. A review gate never suppresses that phase; only an
empty derived scope omits it, and that omission is narrated. This module never
composes the abort branch ``review.mint_workflow`` (pre-execution, C3) is
free to compose for the identical staged fragment — the two callers'
``gate_policy`` closures are never shared or defaulted to each other's
behaviour.

## Permission-mode (contract confirmation, 2026-08-13)

No ``mode:``/permission-mode key is ever placed on an emitted ``agent()``
call — DoE's live-tool capture found no permission-mode carrier on the
``Workflow`` agent-call path at all (options: ``label``, ``phase``,
``schema``, ``model``, ``effort``, ``isolation``, ``agentType``, nothing
else). This module has no code path that emits one.

## Vehicle: EM-dispatched Agent, not a fired-and-forgotten Workflow (live upstream defect)

DoE-claude's ``skills/execute-plan/SKILL.md`` § Vehicle default QUALIFIES
states that a Workflow ``agent()`` spawn is not an ``Agent`` tool call, so
injected ``contract_blocks`` never arrive on that path, and that 33 of 35
coordinator-typed agents carry a ``contract_blocks`` row (git-commit-agent
and atlas-clarity-reviewer carry no such row) — so a plan wave of
coordinator-typed agents belongs on the ``Agent`` path today, not fired
unattended as a Workflow script. Verified OPEN at DoE-claude HEAD
(2026-08-14). The seam is closable and the engine leg for it already exists
here; it is not yet closed — catering arrives once DoE's cutover lands. Until
then, the script this module emits is a durable machine-derived wave-map
artifact an EM dispatches FROM via ``Agent``, one phase at a time — not a
script to run unattended. A future reader whose check shows the seam closed
should DELETE this note rather than cement it, per the same qualifier in the
upstream doctrine text.

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

import yaml

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops._sizing_citation import resolve_sizing_citation
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.pathspec import commit_pathspec, terminal_test_scope
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, read_spine
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow, _normalize_path, build_waves
from coordinator_core.ops.review_mint.compose import compose as compose_review_stages
from coordinator_core.ops.review_mint.roster import RosterFragmentError, Stage, parse_stages
from coordinator_core.ops.workflow_scaffold import _js_string_literal
from coordinator_core.write_guards.block_subagent_plan_body_write import _PLAN_BODY_RE

# Back-compat alias: this module's own review-composition refusal surfaced
# under this name before the C1/C2 extraction (task C4,
# docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md). `roster.
# parse_stages` is now the sole raiser of a malformed-fragment refusal; this
# alias keeps this module's own public surface name stable for any importer
# that still names it, without a second exception class to keep in sync.
ReviewRosterFragmentError = RosterFragmentError

_EXECUTOR_AGENT_TYPE = "coordinator:executor"
_ENRICHER_AGENT_TYPE = "coordinator:enricher"
_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"
_TEST_AGENT_TYPE = "coordinator:test-runner"

_DEFAULT_MODEL = "sonnet"
_AGENT_MODELS = {
    _EXECUTOR_AGENT_TYPE: "sonnet",
    _ENRICHER_AGENT_TYPE: "sonnet",
    _COMMIT_AGENT_TYPE: "haiku",
    _TEST_AGENT_TYPE: "haiku",
}


def _model_opt(agent_type: str) -> str:
    """Compose the ACTIVE ``model:`` opts entry for one ``agentType`` (AC11).

    See module docstring § An ACTIVE model: on every call. An agentType with
    no row falls back to ``_DEFAULT_MODEL`` rather than emitting nothing --
    the invariant is that no call site is ever left model-less.
    """
    return f"model: '{_AGENT_MODELS.get(agent_type, _DEFAULT_MODEL)}'"


# dispatch_emit/emit.py -> dispatch_emit -> ops -> coordinator_core -> repo
# root. Same 3-parents-up derivation `pathspec.py` uses for its own
# `_REPO_ROOT` (this file sits at the identical directory depth) -- kept as
# a separate module-local constant rather than imported, since it is a
# private name on that module's own surface.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Reuses `loe.tshirt`'s six-notch vocabulary (sizing-object.schema.json
# `estimate.tshirt`) as the INPUT and the three-rung vocabulary
# `coordinator:staff-session` already selects on (lightweight/standard/full)
# as the OUTPUT -- see module docstring § Review phases for why this
# mapping lives here (claude-klabauter-owned) rather than a sizing->reviewer table
# (DoE-owned, supplied as a roster fragment, never authored in this repo).
_TSHIRT_TO_REVIEW_TIER: dict[str, str] = {
    "XS": "lightweight",
    "S": "lightweight",
    "M": "standard",
    "L": "standard",
    "XL": "full",
    "XXL": "full",
}

_REVIEW_PHASE_TITLE = "Review"

# (b) Commit-phase placement keyed to wave size: the PM's own n>10
# executors-per-wave marker (see module docstring § Commit-phase placement
# keyed to wave size). A wave at or under this many rows fires exactly one
# commit phase after it, unchanged from before this chunk; a wave over it
# is split into batches of at most this many rows, each with its own
# executor phase immediately followed by its own commit phase -- the same
# commit-phase MECHANISM as always (``_commit_agent_call``), a new
# placement rule only.
_WAVE_COMMIT_BATCH_THRESHOLD = 10


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


def _split_wave_for_commit_placement(
    wave: list[WaveRow], *, threshold: int = _WAVE_COMMIT_BATCH_THRESHOLD
) -> list[list[WaveRow]]:
    """Split ``wave`` into consecutive commit-sized batches (b).

    Returns ``[wave]`` unchanged (one batch) when ``len(wave) <= threshold``
    — the common case, and the one every pre-C5 test already exercises.
    Order-preserving: batch boundaries never reorder rows, only group them.
    See module docstring § Commit-phase placement keyed to wave size.
    """
    if len(wave) <= threshold:
        return [wave]
    return [wave[i : i + threshold] for i in range(0, len(wave), threshold)]


_TEST_PHASE_TITLE = "Scoped test run"
_PREFLIGHT_PHASE_TITLE = "Preflight: commit claimability"


def _row_prompt(row: WaveRow, plan_path: Optional[str] = None) -> str:
    """Compose one executor row's dispatch prompt.

    The prompt MUST name where the row's own spec lives. A title-only
    prompt (``Execute C7: <title>``) leaves the executor to locate its
    spec by guesswork: measured 2026-08-19 against
    ``2026-08-16-one-engine-for-the-whole-box``, one row's executor
    searched ``docs/plans/``, ``state/dispatch-briefs/``,
    ``state/subagent-share/`` and ``archive/``, failed to find the plan,
    and returned BLOCKED-structural; a sibling row in the same wave
    happened to have a greppable title, found the plan, and delivered.
    Spec discovery was therefore a function of how searchable a title
    was, and a row whose executor improvises past that point silently
    violates the negative specs its body carries.

    Negative spec: never emit a prompt that names only ``id`` and
    ``title``. ``plan_path`` is optional solely so pre-existing callers
    that compose from already-derived waves keep working; every caller
    that knows its plan is expected to pass it.
    """
    head = f"Execute {row.id}: {row.title}"
    if not plan_path:
        return head
    return (
        f"{head}\n\n"
        f"Your spec is the row with `id: {row.id}` in the `## Tasks` plan-spine "
        f"of {plan_path} — a fenced ```yaml plan-tasks block. Read that row's "
        "`body`, `writes` and `depends_on` in full before you edit anything, and "
        "follow the body exactly: it carries negative specs, prior-art citations "
        "and constraints this one-line title does not. Do not reconstruct the "
        "spec from the title, from a file search, or from surrounding code — if "
        "you cannot read that row, stop and report BLOCKED rather than "
        "improvising."
    )


def _wave_agent_calls(
    wave: list[WaveRow],
    phase_title: str,
    plan_path: Optional[str] = None,
    results_var: Optional[str] = None,
) -> str:
    """Compose the ``phase()`` + agent-dispatch call(s) for one executor wave.

    A single-row wave emits one ``await agent(...)`` call (a serial gate,
    per the workflow-emitter-contract topology). A multi-row wave emits one
    ``await parallel(...)`` call wrapping one ``agent()`` per row (the
    parallel-wave shape).

    When ``results_var`` is supplied, the call's return value is bound to
    that name (``const {results_var} = await agent(...)`` /
    ``const {results_var} = await parallel([...])``) so the following commit
    phase can splice the returning executor(s)' own reports into its prompt
    as genuine pathspec provenance — see ``_commit_agent_call``. Omitting it
    keeps the pre-existing unbound ``await`` shape (back-compat for any
    caller not threading a commit phase after this wave).
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    binder = f"const {results_var} = " if results_var else ""

    if len(wave) == 1:
        row = wave[0]
        call = (
            f"  {binder}await agent("
            f"{_js_string_literal(_row_prompt(row, plan_path))}, "
            "{ "
            f"label: {_js_string_literal(f'work:{row.id}')}, "
            f"phase: {_js_string_literal(phase_title)}, "
            f"agentType: {_js_string_literal(_row_agent_type(row))}, "
            f"{_model_opt(_row_agent_type(row))} "
            "});"
        )
        return f"{phase_call}\n{call}"

    item_calls = ",\n".join(
        "    () => agent("
        f"{_js_string_literal(_row_prompt(row, plan_path))}, "
        "{ "
        f"label: {_js_string_literal(f'work:{row.id}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_row_agent_type(row))}, "
        f"{_model_opt(_row_agent_type(row))} "
        "})"
        for row in wave
    )
    call = (
        f"  {binder}await parallel([\n"
        f"{item_calls}\n"
        "  ]);"
    )
    return f"{phase_call}\n{call}"


def _escape_for_js_template_literal(text: str) -> str:
    """Escape ``text`` for splicing as LITERAL (non-code) content inside a
    JS template-literal (backtick-quoted) string.

    Only three sequences are special inside a template literal's literal
    text: a bare backtick (would close the literal early), ``${`` (would
    open an interpolation), and a backslash (the escape character itself,
    which must be escaped first so the two escapes below aren't
    double-interpreted). This is deliberately narrower than
    ``workflow_scaffold._js_string_literal`` (which escapes for a
    single-quoted literal) -- a template literal has a different forbidden-
    character set.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )


_PROVENANCE_HEADING = (
    "Pathspec provenance: the pathspec above is this wave's declared "
    "`writes:` scope. The executor report(s) below are, direct from the "
    "executor(s) that just finished, their own touched-files set for this "
    "wave -- verify the handed pathspec against what they actually "
    "reported, and refuse any path in the pathspec that the reports do "
    "not corroborate."
)


#: The machine-checkable success token an emitted commit agent must end its
#: report with. The emitted gate below tests for exactly this string, so a
#: refusal, a crash, or a `null` agent result all fail the same way — CLOSED.
#:
#: Why a token and not the agent's prose: a declined commit and a landed one
#: read almost identically in a progress tree, and the decline reason lives
#: inside an agent result rather than at a phase boundary (example-retrieval-repo-em
#: cross-repo memo, 2026-08-20). A token is the only part of a free-form
#: report a generated script can test without parsing narrative.
#:
#: Bias is deliberate: an agent that commits but omits the token halts a run
#: that did not need halting, which costs a `resumeFromRunId`. The opposite
#: bias loses chunk ids into someone else's commit subject, which is
#: unrecoverable once pushed. Cheap-and-wrong-way-round beats expensive-and-
#: silent.
_COMMIT_LANDED_TOKEN = "COMMIT-LANDED"


def _commit_agent_call(
    pathspec: list[str],
    phase_title: str,
    index: int,
    chunk_ids: list[str] | None = None,
    results_var: Optional[str] = None,
    commit_var: str = "commitResult",
) -> str:
    """Emit the wave's commit-agent call, plus the gate that halts the run
    when that commit did not land (see ``_commit_halt_gate``).

    ``chunk_ids`` is load-bearing, not cosmetic: `close-out-and-stamp`
    joins a commit to a plan chunk on TWO legs -- the ``Deliverable-Id:``
    trailer AND a subject that registers the chunk-id. A wave-scoped
    subject ("Commit wave 2's work") satisfies only the first, so a fully
    executed plan stamps `partial` with every chunk reading uncommitted,
    and the operator has to re-register each row by hand against the
    commit log. Naming the ids here is what makes the emitted run
    close itself out.

    ``results_var``, when supplied, names the JS variable
    ``_wave_agent_calls`` bound to this wave's executor return value(s)
    (see that function's docstring). The prompt then splices those
    results in via ``${JSON.stringify(...)}`` inside a JS template
    literal, under an explicit provenance heading -- see
    ``git-commit-agent.md`` § Pathspec provenance: the committer refuses a
    pathspec whose provenance is a plan chunk's ``writes:`` declaration or
    an EM tree survey, and previously received exactly that (this
    function's pre-existing shape, "Commit wave N's work. Pathspec:
    [...]", states no provenance at all). ``JSON.stringify`` is evaluated
    at RUNTIME inside the emitted script, over whatever the executor(s)
    actually returned -- an executor report containing a stray backtick,
    ``${...}``, or quote is therefore never re-parsed as script syntax:
    template-literal interpolation only concerns itself with the OUTER
    literal's own source text, and the runtime string
    ``JSON.stringify`` produces is spliced in as a value, not re-tokenized.
    The STATIC prompt text this function composes at emit time (subject
    rule, pathspec, provenance heading) is separately escaped via
    ``_escape_for_js_template_literal`` for the same reason: it too now
    lives inside a template literal, not a single-quoted ``_js_string_
    literal`` call.

    Shape of the spliced value: ``agent()``/``parallel()``'s runtime
    return, per the Workflow JS engine, is a plain free-form string --
    the executor's prose report (it names the files it touched, e.g.
    "- `path/to/file.py`: added X ...", but there is no structured
    per-file field). The provenance splice therefore satisfies the
    committer contract's letter -- a returning executor's report is
    genuinely present, and it does name files -- but what the committer
    receives is narrative it must read and reconcile against the handed
    pathspec itself, not a machine-checkable touched-files set. A
    structured shape would require changing the executor contract, not
    this splice; this function does not parse or validate report content.
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    registered = ", ".join(chunk_ids or [])
    subject_rule = (
        f" The commit subject MUST register the chunk id(s) it delivers: {registered}."
        f" Lead the subject with ALL of them, e.g. '{registered}: <what changed>'"
        " -- a wave delivering several chunks needs every id in the subject,"
        " not just the first; close-out registers only the ids it can read there."
        " close-out joins on the subject's chunk-id as well as the"
        " Deliverable-Id trailer; a wave-scoped subject leaves the plan"
        " stamped partial."
        if chunk_ids
        else ""
    )
    static_prompt = (
        f"Commit wave {index + 1}'s work. Pathspec: [{', '.join(pathspec)}]."
        f"{subject_rule}"
        f" When (and ONLY when) the commit has landed, end your report with"
        f" the line '{_COMMIT_LANDED_TOKEN} <sha>'. If you refuse, or the"
        f" commit does not land for any reason, do NOT emit that line —"
        f" state the reason instead. The emitted run halts at this phase"
        f" unless that line is present, so emitting it without a landed"
        f" commit lets the next wave overwrite uncommitted work."
    )

    if results_var:
        static_prompt = f"{static_prompt}\n\n{_PROVENANCE_HEADING}\n\nExecutor report(s):"
        escaped_static = _escape_for_js_template_literal(static_prompt)
        prompt_literal = (
            f"`{escaped_static}\\n${{JSON.stringify({results_var}, null, 2)}}`"
        )
    else:
        prompt_literal = _js_string_literal(static_prompt)

    call = (
        f"  const {commit_var} = await agent("
        f"{prompt_literal}, "
        "{ "
        f"label: {_js_string_literal(f'commit:wave-{index + 1}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_COMMIT_AGENT_TYPE)}, "
        f"{_model_opt(_COMMIT_AGENT_TYPE)} "
        "});"
    )
    gate = _commit_halt_gate(commit_var, phase_title)
    return f"{phase_call}\n{call}\n{gate}"


def _commit_halt_gate(commit_var: str, phase_title: str) -> str:
    """The JS that stops the run when ``commit_var``'s commit did not land.

    `return { halted: ... }` from the script's top-level body is the engine's
    one sanctioned way to stop a run (workflow-orchestration.md); the emitter
    previously generated none for a commit failure, so a declined commit rolled
    straight into the next wave.

    The damage that causes is NOT "work piles up uncommitted". It is that the
    next successful commit touching a shared file absorbs every earlier
    uncommitted chunk under its OWN id — three chunks in one commit registering
    one id, which `close_out_and_stamp` then joins on and stamps `implemented`
    against. It fails looking clean, and the lost id is unrecoverable once the
    absorbing commit is pushed unless the stolen chunks happen to have other
    files left (example-retrieval-repo-em, 2026-08-20; recovered that way by luck, and
    said so).

    A `null` result (the engine's own value for an agent that died or was
    skipped) and a returning-but-tokenless report are treated identically:
    neither proves a commit landed, and this gate only ever asserts the
    positive.
    """
    reason = (
        f"{phase_title} did not land a commit -- halting before the next wave "
        "writes over uncommitted work. Commit this wave's pathspec, then "
        "re-emit and fire a fresh run: resumeFromRunId replays this phase's "
        "cached refusal without re-running it."
    )
    # The test is an ANCHORED match on a whole line, never a substring.
    # Measured 2026-08-21: a bare `.includes(token)` fails OPEN. Subagents may
    # not commit at all (caller-identity enforced), and the refusing agent
    # quoted its own instruction -- "end your report with the line
    # 'COMMIT-LANDED <sha>'" -- back in the refusal. The substring matched, the
    # gate passed, and the next wave ran over an uncommitted one. The bias
    # reasoned about on `_COMMIT_LANDED_TOKEN` above is the right bias; a
    # substring test simply does not implement it, because the token appears in
    # the prompt that every commit agent is holding while it writes its report.
    #
    # Requiring the token at line start AND a real 7-40 hex sha after it means
    # a quotation cannot satisfy the gate: the instruction text carries the
    # literal placeholder `<sha>`, not a sha, and appears mid-sentence.
    return (
        f"  if (!{commit_var} || "
        f"!/^{_COMMIT_LANDED_TOKEN} +[0-9a-f]{{7,40}} *$/m.test(String({commit_var}))) {{\n"
        f"    return {{ halted: {_js_string_literal(reason)} + "
        f'" Agent report: " + String({commit_var} ?? "agent returned null") }};\n'
        "  }"
    )


def _preflight_agent_call(pathspec: list[str], phase_title: str) -> str:
    """Compose the preflight phase's ``phase()`` + ``agent()`` call (AC14).

    Dispatches the SAME ``agentType`` every commit phase later uses
    (``coordinator:git-commit-agent``) against the UNION of every commit
    phase's pathspec, asked to verify claimability only -- no staging, no
    commit. See module docstring § Commit-claimability preflight.
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    prompt = (
        "Preflight only -- do not stage or commit anything. Every path below is "
        "EXPECTED to be unchanged or nonexistent right now: the chunks that write "
        "them have not run yet, so 'no diff' is the correct state and is NOT a "
        "refusal. Report BLOCKED only if a path would be refused by a claim "
        "conflict, an ignore rule, or a guard. Verify that "
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
        f"{_model_opt(_COMMIT_AGENT_TYPE)} "
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
        f"{_model_opt(_TEST_AGENT_TYPE)} "
        "});"
    )
    return f"{phase_call}\n{call}"


def _no_test_scope_narration() -> str:
    """The line composed INSTEAD of the terminal test phase when the spine
    writes no testable surface at all (``terminal_test_scope`` returned an
    empty list).

    A ``log()`` call, never an ``agent()`` call and never a phase: the whole
    point is that no phase runs. It exists so the emitted script is
    self-describing — a reader of the run, and the EM reading its output
    with nobody else present, must be able to tell "this wave was prose, so
    no test was composed" from "a test phase ran and passed." Silence would
    make those two indistinguishable, which is the false-green the original
    AC16 refusal was protecting against and which this narration is what
    replaces.

    Negative spec: never widen this to narrate a PARTIALLY testable wave.
    A wave with any resolved target composes the real phase, and a wave with
    an unmapped ``.py`` never reaches here — ``terminal_test_scope`` raises
    ``NoTestTargetError`` on it.
    """
    message = (
        "No terminal test phase: every path this spine writes is a "
        "non-testable surface (prose/docs), so there is no runnable target "
        "to scope. Absence of a test run here is declared, not a pass."
    )
    return f"  log({_js_string_literal(message)});"


def derive_review_tier(plan_path, *, repo_root: Optional[Path] = None) -> Optional[str]:
    """Derive a review TIER (``lightweight``/``standard``/``full``) from
    ``plan_path``'s own sizing object (a) — never from ``routing.md`` prose
    and never a locally-minted sizing->reviewer table. See module docstring
    § Review phases.

    Reads ONLY ``plan_path``'s frontmatter ``sizing_object:`` citation
    (``frontmatter.primitives.split_frontmatter`` + ``read_fm_field_
    unquoted``, matching ``assert_plan_sizing_citation``'s own frontmatter-
    only read discipline — never the body), then reads that citation's
    sizing-object YAML file's ``estimate.tshirt`` and maps it through
    ``_TSHIRT_TO_REVIEW_TIER``.

    Returns ``None`` (never a fabricated tier) whenever the derivation
    cannot be completed cleanly: the plan file cannot be read, has no
    frontmatter, declares no ``sizing_object:`` (absent or explicit
    ``null``), the cited path does not resolve under ``repo_root``, the
    sizing YAML does not parse to a mapping, or ``estimate``/``estimate.
    tshirt`` is missing. A caller getting ``None`` composes no review phase
    at all rather than guessing a tier — the same fail-soft-by-omission
    posture ``compose_script``'s optional ``review_tier``/``review_roster_
    fragment`` pair uses (see its docstring).

    Raises ``ValueError`` only if ``estimate.tshirt`` IS present but is not
    one of ``_TSHIRT_TO_REVIEW_TIER``'s six schema-enumerated values — never
    expected against a schema-valid sizing object, so this is a fail-loud
    guard against a corrupt record, not a normal branch.
    """
    plan_path = Path(plan_path)
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT

    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        return None

    split = split_frontmatter(text)
    if split is None:
        return None

    cited = read_fm_field_unquoted(split.fm_text, "sizing_object")
    if not cited or cited == "null":
        return None

    # Live-then-archive, containment-checked, in one shared helper
    # (`_sizing_citation.resolve_sizing_citation`) rather than hand-rolled
    # here: a terminal sizing moves to `archive/sizings/<month>/` and its
    # citation is never rewritten, so a literal resolve loses the tier — and
    # a lost tier composes no review phase, silently.
    # (Review: code-reviewer S4-dispatch-emit, P2 finding 1 -- `root / cited`
    # alone does not contain `cited`: `..`-traversal is not normalized by
    # `Path.__truediv__`, and an absolute `cited` silently discards `root`
    # entirely per pathlib semantics. That containment now lives inside the
    # helper, on both the live and the archived arm.)
    resolved = resolve_sizing_citation(root, cited)
    if resolved is None or not resolved.is_file():
        return None
    sizing_path = resolved

    try:
        sizing_doc = yaml.safe_load(sizing_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None

    if not isinstance(sizing_doc, dict):
        return None
    estimate = sizing_doc.get("estimate")
    if not isinstance(estimate, dict):
        return None
    tshirt = estimate.get("tshirt")
    if tshirt is None:
        return None
    if tshirt not in _TSHIRT_TO_REVIEW_TIER:
        raise ValueError(
            f"sizing object {cited!r} declares estimate.tshirt {tshirt!r}, "
            f"not one of {sorted(_TSHIRT_TO_REVIEW_TIER)}"
        )
    return _TSHIRT_TO_REVIEW_TIER[tshirt]


_REVIEW_PROMPT = "Review this plan's completed work."


def _review_gate_policy(stage: Stage, index: int, results: list[tuple[str, str]]) -> str:
    """``dispatch.emit``'s ``gate_policy`` for C2's ``compose()`` (GATE POLICY).

    This caller's commits have already landed by the time a review phase
    runs (post-execution), so a gate abort here must NOT suppress the
    terminal ``coordinator:test-runner`` phase ``compose_script`` appends
    after the review phase. Returning ``""`` unconditionally disarms
    ``compose()``'s early-return branch entirely for every gated stage —
    option (a) from the module docstring's § Review phases / this
    function's callers: stages still compose in order, and a gated stage's
    agent calls still carry the structured-output ``schema``, for
    narration only. No ``return`` is ever spliced, so control always falls
    through to the terminal test phase regardless of verdict.

    ``review.mint_workflow`` (pre-execution, C3) supplies its OWN
    ``gate_policy`` closure with real abort composition — this function is
    ``dispatch.emit``'s alone and is never shared with that caller (see C4's
    body: prompt/phase_title/gate_policy are per-caller, never inherited).
    """
    return ""


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
    review_tier: Optional[str] = None,
    review_roster_fragment: Optional[dict] = None,
    plan_path: Optional[str] = None,
) -> str:
    """Compose one Workflow ``.mjs`` script text from already-derived ``waves``.

    Refuses (``NoWavesError``) if ``waves`` is empty — see module docstring
    § Reuse boundary. Every other refusal (``NoWritesDeclaredError``,
    ``NoTestTargetError``) is raised by ``pathspec.commit_pathspec``/
    ``pathspec.terminal_test_scope`` and propagates unchanged: this function
    adds no derivation of its own.

    A review phase (a) composes ONLY when BOTH ``review_tier`` (this repo's
    data — see ``derive_review_tier``) AND ``review_roster_fragment`` (DoE's
    data — see ``review_mint.roster.parse_stages``) are supplied; either
    alone composes no review phase, never a guessed one. A wave over
    ``_WAVE_COMMIT_BATCH_THRESHOLD`` rows (b) is split into commit-sized
    batches — see module docstring § Commit-phase placement keyed to wave
    size; this changes WHERE ``_commit_agent_call`` fires, never how many
    times overall a whole wave's work is committed for a wave at or under
    the threshold.
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
        batches = _split_wave_for_commit_placement(wave)
        multi_batch = len(batches) > 1

        for batch_index, batch in enumerate(batches):
            if multi_batch:
                suffix = f" (batch {batch_index + 1}/{len(batches)})"
            else:
                suffix = ""

            # (Review: code-reviewer S4-dispatch-emit, P2 finding 2 -- pass
            # `batch`, not the full `wave`: a batch's title must only list
            # the rows that batch actually dispatches.)
            wave_title = f"{_wave_phase_title(index, batch)}{suffix}"
            phase_titles.append(wave_title)
            results_var = (
                f"wave{index + 1}Batch{batch_index + 1}Results"
                if multi_batch
                else f"wave{index + 1}Results"
            )
            body_blocks.append(
                _wave_agent_calls(batch, wave_title, plan_path, results_var)
            )

            batch_pathspec = commit_pathspec(batch)
            commit_title = f"{_commit_phase_title(index)}{suffix}"
            phase_titles.append(commit_title)
            body_blocks.append(
                _commit_agent_call(
                    batch_pathspec,
                    commit_title,
                    index,
                    [row.id for row in batch],
                    results_var,
                    commit_var=f"commit{results_var[0].upper()}{results_var[1:]}",
                )
            )

    if review_tier is not None and review_roster_fragment is not None:
        stages = parse_stages(review_roster_fragment, review_tier)
        for title, block in compose_review_stages(
            stages, _REVIEW_PROMPT, _REVIEW_PHASE_TITLE, _review_gate_policy
        ):
            phase_titles.append(title)
            body_blocks.append(block)

    scope = terminal_test_scope(waves, repo_root=repo_root)
    if scope:
        phase_titles.append(_TEST_PHASE_TITLE)
        body_blocks.append(_test_agent_call(scope, _TEST_PHASE_TITLE))
    else:
        body_blocks.append(_no_test_scope_narration())

    meta_block = _meta_block(name, description, phase_titles)
    body = "\n\n".join(body_blocks)

    # Top-level, never `async function run(ctx) { ... }` -- see module
    # docstring § Top-level body, never a defined-but-uninvoked wrapper.
    return f"{meta_block}\n{body}\n"


def _spec_path_for_prompt(plan_path: Path, repo_root: Optional[Path]) -> Path:
    """The plan path as it should appear in a dispatched executor's prompt.

    Repo-relative, ALWAYS. The dispatched executor resolves the spec from the
    repo root it is already standing in, and an absolute drive-letter path in
    an emitted prompt is exactly the concrete-path-citation hazard AC12 exists
    to keep out of emitted artifacts.

    Negative-spec: this must never return an absolute path. The obvious
    shape -- ``relative_to(repo_root)`` guarded by ``if repo_root is not
    None`` -- silently does exactly that on two reachable paths, and both are
    real rather than theoretical: ``repo_root`` is documented as optional per
    request in ``op.py``, and ``relative_to`` raises ``ValueError`` whenever
    the plan sits on a different mount or drive from the root. Either one puts
    a drive-lettered path into every executor prompt in the emitted script,
    with nothing going red. Found in review, 2026-08-19.

    Ladder, first that yields a relative path wins:
      1. relative to ``repo_root`` when supplied and containing the plan
      2. relative to the process cwd, which for an in-repo invocation is the
         repo root even when the caller passed none
      3. the last three components (``docs/plans/<file>.md`` in practice) --
         still resolvable by an executor standing in the repo, and carrying
         no drive letter
    """
    candidates = []
    if repo_root is not None:
        candidates.append(repo_root)
    try:
        candidates.append(Path.cwd())
    except OSError:
        pass

    for base in candidates:
        try:
            return plan_path.relative_to(base)
        except ValueError:
            continue

    if plan_path.is_absolute():
        parts = plan_path.parts[-3:] if len(plan_path.parts) >= 3 else plan_path.parts[1:]
        return Path(*parts) if parts else Path(plan_path.name)
    return plan_path


def emit_script(
    plan_path,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    repo_root: Optional[Path] = None,
    review_roster_fragment: Optional[dict] = None,
) -> str:
    """Read ``plan_path``'s task spine and compose one Workflow script text.

    Composes the full pipeline: ``spine_read.read_spine`` ->
    ``wave_map.build_waves`` -> ``compose_script``. ``name``/``description``
    default to the plan file's stem and a fixed generic description when
    omitted.

    Also reads ``plan_path``'s frontmatter ONLY (never the body — see
    ``derive_review_tier``) to derive a review tier, which composes a review
    phase (a) if, and only if, a caller also supplies ``review_roster_
    fragment`` (DoE's data — no live fragment exists yet, see module
    docstring § Review phases); omitting it composes no review phase, same
    as before this chunk.
    """
    plan_path = Path(plan_path)
    rows = read_spine(plan_path)
    waves = build_waves(rows)

    resolved_name = name or plan_path.stem
    resolved_description = description or (
        f"Emitted executor/commit/test workflow for {plan_path.stem}"
    )

    review_tier = derive_review_tier(plan_path, repo_root=repo_root)

    spec_path = _spec_path_for_prompt(plan_path, repo_root)

    return compose_script(
        waves,
        name=resolved_name,
        description=resolved_description,
        repo_root=repo_root,
        review_tier=review_tier,
        review_roster_fragment=review_roster_fragment,
        plan_path=spec_path.as_posix(),
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
