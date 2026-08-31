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

import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PlanContext:
    """Plan-level context resolved ONCE per ``emit_script`` call and threaded
    read-only through ``compose_script`` / ``_wave_agent_calls`` to
    ``_row_prompt`` (AC12).

    Never opened or re-derived per row: ``_row_prompt`` only ever splices
    this dataclass's already-resolved fields, never the plan file itself.
    ``goal`` is ``None`` on any plan carrying no ``## Goal`` section (AC13)
    — the caller composing the preamble omits that line entirely rather
    than emitting a placeholder.

    ``exit_criterion`` is the plan's ``prime_exit_criterion.statement``, read
    from FRONTMATTER rather than from a body section — the only field here
    that does not come out of the plan's prose. It defaults to ``None`` so
    every pre-existing construction site stays valid; a plan predating the
    prime-exit-criterion shape simply omits the line.
    """

    title: str
    goal: Optional[str]
    problem_excerpt: Optional[str]
    exit_criterion: Optional[str] = None


# The section-heading vocabulary this module reads out of a plan BODY.
# `## Goal` is C3a's own scaffolded heading (out of C4's write scope --
# this chunk only reads whatever a plan already carries, live or absent).
_GOAL_HEADING = "Goal"
_PROBLEM_HEADING = "Problem"

# A markdown ATX heading line at any level, used as the stop condition for
# a section body -- the next heading of ANY level ends the current section,
# not just a same-level sibling (a `### ` subsection under `## Problem`
# is still part of the Problem section's body).
_NEXT_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)

# The hard character cap on the composed plan-context preamble (AC12's
# "bounded... with an ENFORCED character cap"). The preamble is spliced via
# `_js_string_literal` into EVERY row's `agent(...)` call, so an unbounded
# Problem-section paragraph is paid for on every row of every wave -- see
# module docstring's line-count-is-a-measured-axis discipline
# (`CLAUDE.md` § The brightline). Chosen generously enough to carry a real
# title + goal + one paragraph without truncating the common case, while
# still refusing an unbounded prose blob.
_PLAN_CONTEXT_PREAMBLE_CHAR_CAP = 900

# The scaffolded sentinel `plan.schema.json` excludes from `deliverable_id`
# by negative lookahead -- a plan still carrying it has no id yet.
_DELIVERABLE_ID_PLACEHOLDER_PREFIX = "dlv-placeholder-replace-with"
_TRUNCATION_SUFFIX = "…"


def _plan_section_body(plan_text: str, heading: str) -> Optional[str]:
    """The raw body text under a top-level ``## <heading>`` in ``plan_text``,
    up to (not including) the next markdown heading of any level, or ``None``
    if no such heading exists.

    Matches the FIRST ``## <heading>`` occurrence only -- plan bodies never
    repeat a top-level section heading, and this module does not validate
    that they don't.
    """
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$", plan_text, re.MULTILINE
    )
    if match is None:
        return None
    rest = plan_text[match.end():]
    next_heading = _NEXT_HEADING_RE.search(rest)
    body = rest[: next_heading.start()] if next_heading else rest
    return body.strip("\n")


def _first_paragraph(section_body: str) -> Optional[str]:
    """The first blank-line-delimited paragraph of ``section_body``, with
    interior whitespace collapsed to single spaces -- a wrapped markdown
    paragraph must not carry its source line breaks into a one-line prompt
    preamble. ``None`` if the section is empty."""
    stripped = section_body.strip()
    if not stripped:
        return None
    paragraph = stripped.split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def _plan_title(plan_text: str, fallback: str) -> str:
    """The plan's H1 title (``# <title>``), or ``fallback`` (the plan's file
    stem) when no H1 is present in the body."""
    match = re.search(r"^#\s+(.+?)\s*$", plan_text, re.MULTILINE)
    if match is None:
        return fallback
    return match.group(1)


def derive_plan_context(plan_text: str, *, fallback_title: str) -> PlanContext:
    """Resolve ``PlanContext`` from ``plan_text`` (the plan file's already-
    read full text -- this function never opens a file itself).

    ``goal`` is ``None`` whenever the plan carries no ``## Goal`` section or
    that section's first paragraph is empty (AC13) -- never a placeholder.
    ``problem_excerpt`` is the ``## Problem`` section's first paragraph, or
    ``None`` when no such section exists (every plan today has one, but this
    function stays total rather than assuming it).

    ``exit_criterion`` is read from the plan's FRONTMATTER
    (``prime_exit_criterion.statement``), not from the body, and is ``None``
    whenever the plan has no frontmatter, no ``prime_exit_criterion``, or a
    statement that is absent/empty/not a string. Fail-soft by omission, the
    same posture ``goal`` takes: a preamble that names no criterion is
    correct for a plan that declares none, and a fabricated one would be
    worse than silence.
    """
    goal_body = _plan_section_body(plan_text, _GOAL_HEADING)
    goal = _first_paragraph(goal_body) if goal_body is not None else None

    problem_body = _plan_section_body(plan_text, _PROBLEM_HEADING)
    problem_excerpt = (
        _first_paragraph(problem_body) if problem_body is not None else None
    )

    return PlanContext(
        title=_plan_title(plan_text, fallback_title),
        goal=goal,
        problem_excerpt=problem_excerpt,
        exit_criterion=_prime_exit_criterion_statement(plan_text),
    )


def _prime_exit_criterion_statement(plan_text: str) -> Optional[str]:
    """``prime_exit_criterion.statement`` from ``plan_text``'s frontmatter,
    whitespace-collapsed to one line, or ``None``.

    Parses the frontmatter block as YAML rather than reaching for
    ``read_fm_field_unquoted``: that helper reads a TOP-LEVEL scalar, and
    this field is nested one level down. Every failure mode — no
    frontmatter, unparseable YAML, a non-mapping document, a
    ``prime_exit_criterion`` that is absent or not a mapping, a
    ``statement`` that is missing, empty, or not a string — returns
    ``None``. This function never raises on a malformed plan: an emitted
    workflow losing one preamble line is recoverable, an emit that dies on
    a plan's frontmatter is not.
    """
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    block = doc.get("prime_exit_criterion")
    if not isinstance(block, dict):
        return None
    statement = block.get("statement")
    if not isinstance(statement, str):
        return None
    collapsed = " ".join(statement.split())
    return collapsed or None


def _plan_deliverable_id(plan_text: str) -> Optional[str]:
    """The plan's top-level frontmatter ``deliverable_id``, or ``None``.

    Parses the frontmatter as YAML for the same reason
    ``_prime_exit_criterion_statement`` does, and is fail-soft in every
    direction: no frontmatter, unparseable YAML, a non-mapping document, an
    absent, null, non-string, or empty ``deliverable_id`` all return
    ``None``.

    The scaffolded placeholder (``dlv-placeholder-replace-with-...``, which
    ``plan.schema.json`` excludes by negative lookahead) is rejected too, as
    is any value without the ``dlv-`` prefix that schema requires. A commit
    prompt naming no id costs one hand-written ``disposition_ref``; a commit
    prompt naming a placeholder stamps unrewritable shared history with an
    id that joins to nothing.
    """
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    value = doc.get("deliverable_id")
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate.startswith("dlv-"):
        return None
    if candidate.startswith(_DELIVERABLE_ID_PLACEHOLDER_PREFIX):
        return None
    return candidate


def _plan_context_preamble(context: PlanContext) -> str:
    """Compose the plan-context preamble spliced ahead of a row's own
    dispatch prompt (AC12/AC13), bounded to
    ``_PLAN_CONTEXT_PREAMBLE_CHAR_CAP`` characters (enforced below, never
    left to instruction alone).

    Line order: title, then ``Goal:`` ONLY when ``context.goal`` is present
    (AC13 -- omitted entirely, never a placeholder line), then ``Exit
    criterion:`` when the plan declares one, then the Problem excerpt when
    present.

    The exit criterion sits AHEAD of the Problem excerpt deliberately: the
    Problem says what was wrong, the criterion says what must be observably
    true for the row's work to have landed, and an executor that reads only
    the first two lines should have the second of those. An executor that
    never learns its plan's criterion is the one that builds a thing that is
    wrong in a new way -- see
    cross-repo/archive/2026-08-27-doe-claude-em-prime-exit-criterion-settled-shape.md.

    Named external seam: DoE-claude's ``coordinator/bin/emit-dispatch-workflow.py``
    monkeypatches ``_row_prompt`` wholesale and calls THIS function to compose the
    same preamble ahead of its own row body. That is the sanctioned shape -- it is
    the only way an outside composer inherits
    ``_PLAN_CONTEXT_PREAMBLE_CHAR_CAP`` rather than re-deriving a cap that then
    drifts. Negative spec: do not narrow or rename this signature without
    notifying that shim; a widening here is what broke it once already.
    """
    lines = [f"Plan: {context.title}"]
    if context.goal:
        lines.append(f"Goal: {context.goal}")
    if context.exit_criterion:
        lines.append(f"Exit criterion: {context.exit_criterion}")
    if context.problem_excerpt:
        lines.append(f"Problem: {context.problem_excerpt}")
    preamble = "\n".join(lines)
    if len(preamble) > _PLAN_CONTEXT_PREAMBLE_CHAR_CAP:
        cut = _PLAN_CONTEXT_PREAMBLE_CHAR_CAP - len(_TRUNCATION_SUFFIX)
        preamble = preamble[:cut] + _TRUNCATION_SUFFIX
    return preamble


def _row_prompt(
    row: WaveRow,
    plan_path: Optional[str] = None,
    plan_context: Optional[PlanContext] = None,
) -> str:
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

    ``plan_context`` (AC12) is an already-resolved ``PlanContext`` -- this
    function never opens or parses the plan itself to obtain one. When
    supplied, its preamble (``_plan_context_preamble``) is spliced ahead of
    everything else so a dispatched executor learns which plan it is inside
    and what that plan is for before it reads its own row's spec pointer.
    Omitted (``None``) keeps the pre-existing shape unchanged, for any
    caller not yet threading plan context.
    """
    head = f"Execute {row.id}: {row.title}"
    if not plan_path:
        return head
    body = (
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
    if plan_context is None:
        return body
    return f"{_plan_context_preamble(plan_context)}\n\n{body}"


def _wave_agent_calls(
    wave: list[WaveRow],
    phase_title: str,
    plan_path: Optional[str] = None,
    results_var: Optional[str] = None,
    plan_context: Optional[PlanContext] = None,
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

    ``plan_context`` (AC12) is forwarded, unopened and unparsed, straight to
    ``_row_prompt`` for every row in the wave — see that function's docstring.
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    binder = f"const {results_var} = " if results_var else ""

    if len(wave) == 1:
        row = wave[0]
        call = (
            f"  {binder}await agent("
            f"{_js_string_literal(_row_prompt(row, plan_path, plan_context))}, "
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
        f"{_js_string_literal(_row_prompt(row, plan_path, plan_context))}, "
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


#: Dispatch-layer bookkeeping surfaces an executor may legitimately write
#: outside its wave's declared `writes:`. Membership here is the whole
#: discriminator between a sidecar to ignore and stranded chunk work to halt
#: on, so it is a POSITIVE allowlist and not a heuristic: an extra path that
#: matches nothing here halts. Adding a prefix buys back silence on exactly
#: that prefix and nothing else -- widen it only for a surface the dispatch
#: layer itself writes, never for a surface a chunk might.
_BOOKKEEPING_PREFIXES: tuple[str, ...] = ("state/subagent-share/",)

_BOOKKEEPING_PREFIX_RENDER = ", ".join(f"`{prefix}**`" for prefix in _BOOKKEEPING_PREFIXES)


#: Why each negative clause below is spelled out rather than left to judgment:
#: every one names a refusal measured live, where the committer was correct
#: about the fact and wrong about what it implied. Executing one plan
#: (`pln-the-discriminators-that-alread-1545b5`, 2026-08-27) produced five
#: halts, four of them from this prompt and none a real problem: an executor's
#: own `state/subagent-share/**` sidecar read as pathspec divergence; ~30
#: peer-staged paths in a shared index read as "would be swept into my commit";
#: a declared write target the chunk legitimately did not change read as a
#: guard failure. Each cost a `resumeFromRunId`.
#:
#: The reverse direction is NOT symmetric with those four, and the block does
#: police it: a reported-written path outside the pathspec and outside
#: `_BOOKKEEPING_PREFIXES` is stranded chunk work. Measured on
#: `docs/plans/2026-08-30-who-pushes-and-when.md` -- C7's `writes:` went 3 -> 8
#: paths and C8's 4 -> 7 across three halts, the executors wrote all of them
#: and reported green, and the commit agents committed only the original 3 and
#: 4. Eight files sat uncommitted while their implementation halves landed, so
#: HEAD was red on a shared branch and nothing in the run said so. That is why
#: the exception is a positive allowlist rather than a blanket flip: a flip
#: re-buys the four halts above, and a commit phase that halts four times in
#: five is a phase operators learn to override.
_PROVENANCE_HEADING = (
    "Pathspec provenance: the pathspec above is this wave's declared "
    "`writes:` scope. The executor report(s) below are, direct from the "
    "executor(s) that just finished, their own touched-files set for this "
    "wave -- verify the handed pathspec against what they actually "
    "reported, and refuse any path in the pathspec that the reports do "
    "not corroborate."
    "\n\nThe check is ONE-DIRECTIONAL BY DEFAULT, with one named exception. "
    "A path the reports name that is NOT in the pathspec is outside this "
    "wave's declared `writes:` scope. If it falls under a DISPATCH-LAYER "
    f"BOOKKEEPING prefix -- {_BOOKKEEPING_PREFIX_RENDER} -- it is not chunk "
    "work: leave it uncommitted, do not refuse over it, do not mention it. "
    "\n\nAny OTHER reported-written path absent from the pathspec IS a "
    "divergence and you must STOP. It is chunk work this wave produced, and "
    "committing without it strands it: its implementation half lands, the "
    "rest sits uncommitted, and HEAD goes red on a branch many sessions "
    "share -- silently, because every chunk still reports DONE and this "
    "commit still succeeds. Do NOT widen the pathspec yourself and do NOT "
    "commit the extra path: the pathspec is the spine's declared scope and "
    "only the spine may widen it. Report instead, naming every such path "
    "verbatim, in this shape -- 'these were written but are not in this "
    "wave's declared `writes:` -- widen the spine row and restamp, or "
    "confirm they are bookkeeping'. Emit no success token."
    "\n\nSHARED TREE: this repo is worked by many concurrent sessions, and "
    "the index routinely holds staged paths belonging to peers. That is the "
    "normal state, not a divergence and not a refusal condition -- your "
    "scoped-commit route commits only the paths in the pathspec and cannot "
    "sweep peer-staged work into your commit. Never unstage, revert, or "
    "commit a peer's paths, and never ask for the index to be cleared."
    "\n\nA CLAIM CAN REFUSE YOU, BUT ONLY A GUARD RAISES IT -- AND A DEAD "
    "HOLDER'S CLAIM IS REAPABLE. Two layers, and they answer differently. "
    "`commit_paths` performs NO ownership or claim check -- see `coordinator_core/"
    "git/commit.py`'s guarded-seam header, which records that this route cannot "
    "reach the ownership leg at all. But a PreToolUse guard sits IN FRONT of the "
    "route and does refuse on a claim, before `commit_paths` is ever called: "
    "`BLOCKED: git-commit-agent commits only via a non-sweeping, in-scope "
    "pathspec ... Argv shape was fine; denied on path scope: '<path>' (claimed "
    "by session ...`. If you are holding that text, it is the GUARD declining, "
    "not your inference -- do not conclude the claim is imaginary because the "
    "route does not check claims. THE RECOVERY, and it needs no EM: run "
    "`session-claim-cli who-claims-path <path>`, which prints one line per "
    "holder with a live/dead verdict already resolved for you. For a dead "
    "holder, `session-claim-cli clear-claim-if-dead artifact <path>` releases "
    "it -- that verb is a no-op against a LIVE holder by construction, so it "
    "cannot reap a working peer and is safe to run without asking. Then "
    "re-issue the commit. Measured 2026-08-31 on this exact surface: two waves "
    "of one run halted here, one on a dead session's claim and one on an "
    "orphan record with no session at all, and both denials arrived truncated "
    "mid-token so the liveness verdict the guard had composed never reached "
    "the agent. Before treating a claim "
    "as blocking, establish two things: (1) the holder is ALIVE -- a recorded "
    "pid absent from the process table, or a `meta.json` `last_activity` hours "
    "old, is a stale claim from a dead session, and this repo carries dozens of "
    "them; (2) the holder actually touched THE PATH at issue -- read its "
    "`touch-record.jsonl` rather than generalising one hit across your whole "
    "pathspec. Measured 2026-08-31: a wave was declined in full over a claim held "
    "by a session 13 hours idle with both pids dead that had touched ONE of seven "
    "paths; the sanctioned route then committed all seven without complaint. If "
    "both conditions genuinely hold, refuse ONLY the claimed paths and commit the "
    "remainder -- a live peer editing one file is not a reason to strand six."
    "\n\nUNCHANGED DECLARED PATHS: a path in the pathspec that this wave's "
    "executor legitimately did not change (reported as examined-but-unchanged) "
    "must be DROPPED from the pathspec and the remainder committed. A chunk "
    "whose diagnosis did not license an edit to one of its declared write "
    "targets is an ordinary outcome. Refuse only if NO declared path changed."
    "\n\nALREADY COMMITTED: a run resumed after an edit re-runs commit phases "
    "that already succeeded. Tracked-and-clean alone is NOT evidence of that "
    "-- it is equally true of a path this run never touched. Before "
    "reporting a landed wave satisfied, find THIS wave's own commit via "
    "`git log` (chunk id in the subject or `Deliverable-Id:` trailer) and "
    "report that commit's sha with the success token. No matching commit: "
    "investigate as a real failure, do not report success on clean-tree "
    "alone."
    "\n\nTHE CALL RETURNS THE SHA: the route is "
    "`coordinator_core.git.commit.commit_paths`, which returns a "
    "`CommitOutcome` whose `.sha` IS the landed commit, or raises "
    "`CommitRefused`. There is no `exit_code`/`landed`/`committed_sha` "
    "triple to read, and `run_commit_pipeline` no longer exists -- a "
    "`ModuleNotFoundError` importing it is a stale reference in whatever "
    "told you to call it, never evidence the route is unavailable."
    "\n\nTHE SHA IS NOT THE WHOLE OUTCOME -- READ `.no_delta` TOO. A "
    "non-empty `CommitOutcome.no_delta` names paths YOU DECLARED that "
    "contributed nothing, because their bytes already matched HEAD. The "
    "commit is real and the sha is real; those paths are simply not in it. "
    "The usual cause is a hook or a peer having committed that path moments "
    "before you, and the path that goes missing is disproportionately the "
    "one the wave existed to deliver -- a plan `.md` already committed by a "
    "status-transition hook is the measured case (DoE-claude `874cf35dd`: "
    "five paths declared, four landed, and the fifth was the point). "
    "Reporting only the sha there reports delivery of something you did not "
    "deliver. Name every `no_delta` path in your report, ABOVE the success "
    "token line, and say it did not land in this commit. This is a REPORT, "
    "never a refusal: a commit that delivers some of its pathspec is "
    "legitimate and must still be reported landed."
    "\n\nA `TypeError` ON THE CALL IS A WRONG KEYWORD, NOT AN ABSENT ROUTE. "
    "The repo argument is `repo` (positional-or-keyword), NOT `repo_root`; "
    "the signature is "
    "`commit_paths(repo, paths, message, *, deleted_paths=(), ...)`. "
    "Correct the call and re-issue it. Do NOT fall through to a raw "
    "`git commit` -- that is denied to you by caller identity, and the "
    "denial is not a finding about this route."
    "\n\nA `FilterUnsupported` IS A MISSING `blob_fallback`, NOT AN ABSENT "
    "ROUTE EITHER. `N path(s) need a checkin conversion this module does not "
    "reproduce ... and no blob_fallback was supplied` means your pathspec "
    "holds a path whose blob sha `commit_paths` refuses to guess -- a "
    "`text`/`eol=`-attributed path carrying CR bytes, an LFS path, or an "
    "unresolved `[attr]` macro -- rather than commit bytes git disagrees "
    "with."
    "\n\nWHICH paths those are is a property of the TARGET REPO's "
    "`.gitattributes`, and it does NOT travel between repos. Do not predict "
    "it from file extension: a repo opening with a blanket `* text=auto` "
    "makes ordinary `.md` refuse, while a repo that pins `eol=lf` (or "
    "attributes nothing) commits the same extension in process. Both shapes "
    "are live in this fleet, measured 2026-08-30. `git check-attr text eol "
    "-- <path>` is how you settle it for a specific path if you need to "
    "know."
    "\n\nSo pass the fallback UNCONDITIONALLY -- never on a prediction about "
    "your pathspec's composition. It costs one batched `git hash-object` "
    "spawn, and only for the paths the in-process check actually refuses:"
    "\n\n    from functools import partial"
    "\n    from coordinator_core.git.commit import hash_worktree_blobs_via_spawn"
    "\n    commit_paths(repo, paths, message, deleted_paths=deleted,"
    "\n                 blob_fallback=partial(hash_worktree_blobs_via_spawn,"
    "\n                                       cwd=repo))"
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
    deliverable_id: Optional[str] = None,
) -> str:
    """Emit the wave's commit-agent call, plus the gate that halts the run
    when that commit did not land (see ``_commit_halt_gate``).

    ``chunk_ids`` is load-bearing, not cosmetic: `close-out-and-stamp`
    verifies a commit against a plan chunk via pure sha-ancestry evidence
    -- a `disposition: coded` spine row's own `disposition_ref` field
    (see `close_out_and_stamp.py`'s module docstring), never a commit
    message or subject parse. A wave-scoped subject ("Commit wave 2's
    work") carries no chunk-id registration itself; naming the ids in
    the prompt is what lets the committing agent write a correct
    `disposition_ref` back onto each chunk's spine row, which is what
    makes the emitted run close itself out.

    ``deliverable_id`` carries the identical stakes, on a separate axis
    (the Deliverable-Id trailer, attached by the commit route itself --
    see ``deliverable_rule`` below -- rather than the subject/spine
    join above): with no id named here the committer resolves one from
    whatever ambient session state it finds, which is a stale
    id belonging to an unrelated workstream as often as not (observed
    2026-08-19: `302ca5430` and `dde488e12` both landed carrying
    ``dlv-git-amplification-hitlist-burn-down-391b0f`` while executing a
    plan whose own id was
    ``dlv-the-windows-commit-hook-starts-python-on-99b845``). Shared
    history cannot be rewritten to correct a trailer after the fact --
    the only recovery is a hand-written per-row ``disposition_ref`` --
    so the emitter, which knows the id, names it rather than leaving it
    to be inferred.

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
        " not just the first; a resumed agent finds its wave's own commit by"
        " chunk id in the subject."
        if chunk_ids
        else ""
    )
    deliverable_rule = (
        " A Deliverable-Id trailer is attached to this commit automatically"
        " by the commit route itself (ceremony.commit_v2's apply_missing_trailers"
        " call, not a git hook) -- do not pass a flag for it and do"
        " not hand-write one into the message body. If the trailer resolves"
        " to an id you did not expect, report it; that is never grounds to"
        " amend, reset, or re-commit."
        if deliverable_id
        else ""
    )
    static_prompt = (
        f"Commit wave {index + 1}'s work. Pathspec: [{', '.join(pathspec)}]."
        f"{subject_rule}{deliverable_rule}"
        f" If `CommitOutcome.no_delta` comes back non-empty, list those paths"
        f" first and say they contributed nothing to this commit -- they are"
        f" paths you declared that were already at HEAD, and reporting only"
        f" the sha would report them as delivered."
        f" When (and ONLY when) the commit has landed, end your report with"
        f" the line '{_COMMIT_LANDED_TOKEN} <sha>'. If you refuse, or the"
        f" commit does not land for any reason, do NOT emit that line —"
        f" state the reason instead. The emitted run halts at this phase"
        f" unless that line is present, so emitting it without a landed"
        f" commit lets the next wave overwrite uncommitted work."
        # Review: overengineering-reviewer flagged this sentence as
        # unconditional payload for a reader who cannot act on it —
        # dispositioned "accepted" in the sidecar, but the dispatching EM
        # overrode: three separate repos have misattributed this exact
        # wording to DoE-claude's CLI, and this sentence's audience is
        # precisely the reader who wants it corrected. Left in place by EM
        # decision, not an oversight.
        " This commit-phase prompt is composed by"
        " coordinator_core/ops/dispatch_emit/emit.py."
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
    # The recovery text is the whole point of the halt: an operator who
    # cannot act on it re-emits, and a re-emit is the ONE move that loses
    # work silently -- `spine_read` correctly drops the chunks that already
    # landed, so the second script is narrower and is indistinguishable on
    # disk from one always meant to be partial (doe-claude-em cross-repo
    # memo, 2026-08-30). Naming the resume call here is what puts the
    # correct move in front of the EM at the moment they need it.
    #
    # Editing the call is NOT optional advice: resume serves the longest
    # UNCHANGED prefix of agent() calls from cache, so relaunching this
    # script untouched replays this phase's cached refusal and halts again
    # at the same gate.
    reason = (
        f"{phase_title} did not land a commit -- halting before the next wave "
        "writes over uncommitted work. RECOVERY IS RESUME, NEVER RE-EMIT: a "
        "second emit re-reads the spine, which correctly excludes the chunks "
        "that DID land, so the new script is silently narrower than this one "
        "(tripwire A-SECOND-EMIT-AFTER-A-PARTIAL-RUN-NARROWS-SILENTLY). "
        "To resume: commit this wave's pathspec yourself, then EDIT this "
        "phase's commit-agent step in the persisted script -- an unchanged call "
        "is served from cache and replays this same refusal -- and relaunch "
        "Workflow({scriptPath, resumeFromRunId}). Both values are in the tool "
        "result that launched this run; every earlier phase is cached and is "
        "not re-paid. resumeFromRunId is same-session-only: if that session "
        "is gone, a re-emit is the remaining move, but expect a narrower "
        "script and verify the dropped waves are exactly the ones that landed."
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
    #
    # The optional `[*_]{0,2}` runs are NOT a relaxation of that anchoring --
    # they are the measured shape of a report that DID commit. Census of 378
    # commit-agent outcomes across 653 workflow journals (2026-08-30, see
    # state/audits/2026-08-30-what-the-commit-halts-actually-were.md): of 18
    # halts, FOUR were reports whose token line read `**COMMIT-LANDED <sha>**`.
    # All four of those commits are in this repo's history (5e4a76ea70,
    # ca1ccc6019, 5eb6df2ece, ae9607e410) -- the runs were killed AFTER their
    # work had landed. At 22% that is the single largest halt class, and each
    # one is a re-fire the operator paid for nothing.
    #
    # Emphasis cannot reopen the 2026-08-21 fail-open, because what closed it
    # was the hex-sha requirement, not the absence of asterisks: the
    # instruction every commit agent is holding while it writes carries the
    # literal placeholder `<sha>`, which is not hex however it is decorated.
    return (
        f"  if (!{commit_var} || "
        f"!/^[*_]{{0,2}}{_COMMIT_LANDED_TOKEN}[*_]{{0,2}} +[*_]{{0,2}}"
        f"[0-9a-f]{{7,40}}[*_]{{0,2}} *$/m.test(String({commit_var}))) {{\n"
        f"    return {{ halted: {_js_string_literal(reason)} + "
        f'" Agent report: " + String({commit_var} ?? "agent returned null") }};\n'
        "  }"
    )


_PREFLIGHT_BLOCKED_TOKEN = "PREFLIGHT-BLOCKED"


def _preflight_agent_call(pathspec: list[str], phase_title: str) -> str:
    """Compose the preflight phase's ``phase()`` + ``agent()`` call (AC14).

    Dispatches the SAME ``agentType`` every commit phase later uses
    (``coordinator:git-commit-agent``) against the UNION of every commit
    phase's pathspec, asked to verify claimability only -- no staging, no
    commit. See module docstring § Commit-claimability preflight.

    The call's result is bound to a variable and gated by
    ``_preflight_halt_gate`` (see there for why an unbound ``await agent(...)``
    is not decorative-only -- it discards the one verdict the phase exists to
    produce). Mirrors ``_commit_agent_call``/``_commit_halt_gate``'s shape.
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
        "before any further phase runs -- and end your report with the line "
        f"'{_PREFLIGHT_BLOCKED_TOKEN} <reason>'. If every path is claimable, "
        "do NOT emit that line."
    )
    preflight_var = "preflightResult"
    call = (
        f"  const {preflight_var} = await agent("
        f"{_js_string_literal(prompt)}, "
        "{ "
        f"label: {_js_string_literal('preflight:commit-claimability')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_COMMIT_AGENT_TYPE)}, "
        f"{_model_opt(_COMMIT_AGENT_TYPE)} "
        "});"
    )
    gate = _preflight_halt_gate(preflight_var, phase_title)
    return f"{phase_call}\n{call}\n{gate}"


def _preflight_halt_gate(preflight_var: str, phase_title: str) -> str:
    """The JS that stops the run when ``preflight_var``'s report is BLOCKED.

    Mirrors ``_commit_halt_gate``'s shape and its anchored-regex lesson: a
    bare ``.includes(token)`` fails OPEN, because the prompt itself carries
    the literal token text ("end your report with the line
    '{_PREFLIGHT_BLOCKED_TOKEN} <reason>'"), so an agent that merely echoes
    or quotes its own instructions back would satisfy a substring test
    without ever having found a refused path.

    The match requires the token at the start of a line, followed by a real
    reason (one or more non-newline characters) -- the prompt's own
    placeholder text is the literal string ``<reason>``, but the anchor is
    line-start plus the token, not the placeholder shape, so this does not
    depend on the agent avoiding that literal string.
    """
    reason = (
        f"{phase_title} reported a claimability blocker -- halting before "
        "any wave writes over a path that would be refused."
    )
    return (
        f"  if (/^[*_]{{0,2}}{_PREFLIGHT_BLOCKED_TOKEN}[*_]{{0,2}}"
        f" +\\S.*$/m.test(String({preflight_var} ?? \"\"))) {{\n"
        f"    return {{ halted: {_js_string_literal(reason)} + "
        f'" Agent report: " + String({preflight_var} ?? "agent returned null") }};\n'
        "  }"
    )


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


def derive_review_tier(
    plan_path, *, repo_root: Optional[Path] = None, plan_text: Optional[str] = None
) -> Optional[str]:
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

    ``plan_text``, when supplied, is used AS-IS instead of this function
    opening ``plan_path`` itself (AC16) — ``emit_script`` reads the plan
    file exactly once and passes that text down here and to
    ``derive_plan_context`` so the plan is never opened twice for one
    ``emit_script`` call. Omitted, this function reads the file itself —
    unchanged behaviour for every pre-existing caller.

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

    if plan_text is not None:
        text = plan_text
    else:
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
    plan_context: Optional[PlanContext] = None,
    deliverable_id: Optional[str] = None,
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

    ``plan_context`` (AC12), when supplied, is forwarded unopened to every
    ``_wave_agent_calls`` call so each row's prompt carries the plan-context
    preamble — see ``PlanContext``/``_row_prompt``. This function never
    resolves one itself; ``emit_script`` is the sole resolution site.

    ``deliverable_id`` is threaded to every ``_commit_agent_call`` the same
    way each batch's chunk ids are -- the two together are the join
    ``close-out-and-stamp`` needs. It is likewise resolved only in
    ``emit_script``; a plan declaring none emits commit prompts that name
    none, never a guessed or placeholder id.
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
                _wave_agent_calls(
                    batch, wave_title, plan_path, results_var, plan_context
                )
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
                    deliverable_id=deliverable_id,
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

    Reads ``plan_path``'s full text (frontmatter AND body) exactly ONCE
    (AC16) — ``read_spine`` above already opens and reads the file to locate
    its task-spine block, so this is the file's second and ONLY OTHER read,
    consolidating what would otherwise be two separate re-reads (one for
    ``derive_review_tier``'s frontmatter-only ``sizing_object:`` citation, a
    second for ``derive_plan_context``'s ``## Goal``/``## Problem`` body
    sections and its frontmatter ``prime_exit_criterion.statement``) into
    the ONE ``plan_path.read_text()`` call below, whose
    result both derivations consume via their ``plan_text``/``plan_text``
    parameters. Neither derivation, nor any per-row prompt composition
    downstream, opens the plan file again — this corrects the module's
    prior claim (frontmatter ONLY, never the body) now that AC12's
    plan-context preamble reads the Problem section's first paragraph and,
    when present, a ``## Goal`` section.

    A review phase (a) composes if, and only if, a caller also supplies
    ``review_roster_fragment`` (DoE's data — no live fragment exists yet,
    see module docstring § Review phases); omitting it composes no review
    phase, same as before this chunk.

    ``plan_context`` (AC12) is resolved here, once, and passed to
    ``compose_script`` -> ``_wave_agent_calls`` -> ``_row_prompt`` — no
    downstream function opens or re-parses the plan to obtain it. The
    plan's ``deliverable_id`` is resolved from the same already-read text
    and passed alongside it, reaching every commit prompt via
    ``compose_script`` -> ``_commit_agent_call``.
    """
    plan_path = Path(plan_path)
    rows = read_spine(plan_path)
    waves = build_waves(rows)

    resolved_name = name or plan_path.stem
    resolved_description = description or (
        f"Emitted executor/commit/test workflow for {plan_path.stem}"
    )

    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError:
        plan_text = None

    review_tier = derive_review_tier(plan_path, repo_root=repo_root, plan_text=plan_text)

    spec_path = _spec_path_for_prompt(plan_path, repo_root)

    plan_context = derive_plan_context(
        plan_text if plan_text is not None else "",
        fallback_title=plan_path.stem,
    )

    deliverable_id = _plan_deliverable_id(plan_text) if plan_text else None

    return compose_script(
        waves,
        name=resolved_name,
        description=resolved_description,
        repo_root=repo_root,
        review_tier=review_tier,
        review_roster_fragment=review_roster_fragment,
        plan_path=spec_path.as_posix(),
        plan_context=plan_context,
        deliverable_id=deliverable_id,
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
