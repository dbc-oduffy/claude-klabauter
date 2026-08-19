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
a machine-readable fragment (shape documented at ``_reviewers_for_tier``)
that this module consumes and never authors. Composing a review phase
therefore needs BOTH a derived tier (this repo's data) AND a supplied
fragment (DoE's data) — either alone composes nothing.

The DoE-side roster fragment does not exist yet. This module is built and
tested against a fixture fragment of its own making
(``tests/test_emit.py``'s review-phase tests) — only LIVE consumption of a
real DoE fragment waits on the fragment landing; tier derivation and
fragment-shaped composition are real and tested today.

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

import yaml

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops._path_guard import contained_path
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


class ReviewRosterFragmentError(ValueError):
    """Raised when a supplied review-roster fragment lacks the expected shape.

    See ``_reviewers_for_tier``'s docstring for the fragment shape this
    error is checked against.
    """


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

    # (Review: code-reviewer S4-dispatch-emit, P2 finding 1 -- `root / cited`
    # alone does not contain `cited`: `..`-traversal is not normalized by
    # `Path.__truediv__`, and an absolute `cited` silently discards `root`
    # entirely per pathlib semantics. Reuses the shared containment helper
    # rather than hand-rolling a check, matching the sibling `pathspec.py`
    # containment shape.)
    resolved = contained_path(root / cited, [root])
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


def _reviewers_for_tier(fragment: dict, tier: str) -> list[str]:
    """Look up ``tier``'s reviewer ``agentType`` list in a DoE-supplied
    review-roster fragment.

    FRAGMENT SHAPE (DoE-owned; this repo consumes it and never authors the
    mapping — see module docstring § Review phases)::

        {
          "schema": "review-roster-fragment",
          "tiers": {
            "lightweight": ["coordinator:code-reviewer"],
            "standard": ["coordinator:code-reviewer", "coordinator:review-integrator"],
            "full": [
              "coordinator:code-reviewer",
              "coordinator:review-integrator",
              "coordinator:staff-eng"
            ]
          }
        }

    ``tiers`` keys are exactly the three tier names ``_TSHIRT_TO_REVIEW_
    TIER`` derives (``lightweight``/``standard``/``full``) — the same
    vocabulary ``coordinator:staff-session`` already selects a tier on.
    Each value is a non-empty list of ``agentType`` strings, composed onto
    a review phase the same way a commit phase's ``agentType`` is composed
    (``_review_phase_calls``) — no new firing mechanism.

    Raises ``ReviewRosterFragmentError`` naming what is missing — a
    fragment with no ``tiers`` mapping, or a ``tiers`` mapping missing
    ``tier``'s key, or an empty list at that key — never a silent empty
    reviewer list, which would compose a review phase dispatching nobody.
    """
    tiers = fragment.get("tiers") if isinstance(fragment, dict) else None
    if not isinstance(tiers, dict):
        raise ReviewRosterFragmentError(
            "review roster fragment carries no 'tiers' mapping"
        )
    reviewers = tiers.get(tier)
    if not reviewers:
        raise ReviewRosterFragmentError(
            f"review roster fragment declares no reviewers for tier {tier!r}"
        )
    # A schema_version >= 2 fragment maps each tier to {"stages": [...]}, not
    # to a flat reviewer list. `list()` over that mapping yields its KEYS --
    # ``['stages']`` -- which composes a review phase dispatching a
    # nonexistent ``agentType: 'stages'``. Silently wrong beats loudly wrong
    # for nobody, so refuse: stage consumption is deliberately not implemented
    # here (it needs gate/abort composition this module has no concept of).
    if not isinstance(reviewers, list):
        raise ReviewRosterFragmentError(
            f"review roster fragment tier {tier!r} is not a flat reviewer "
            f"list (schema_version {fragment.get('schema_version', 1)!r}); "
            "this emitter consumes the flat shape only and cannot compose "
            "the staged shape's gates"
        )
    return list(reviewers)


def _review_phase_calls(reviewers: list[str], phase_title: str) -> str:
    """Compose the review phase's ``phase()`` + agent-dispatch call(s) (a).

    One ``agent()`` call per reviewer ``agentType`` — a single reviewer
    emits a plain serial ``await agent(...)``, more than one emits
    ``await parallel([...])``, the identical single-vs-multi shape
    ``_wave_agent_calls`` already uses for an executor wave. Composed on
    the SAME phase mechanism as every other phase in this module (no new
    firing mechanism) — see module docstring § Review phases.

    NEGATIVE SPEC — reviewer calls carry NO ``model:`` key, unlike every
    other call this module composes. The roster fragment supplies reviewer
    ``agentType``s, and an agent definition pins its own tier in its
    frontmatter (the persona agents are ``model: opus`` by charter). The
    Workflow tool's ``opts.model`` takes PRECEDENCE over that frontmatter,
    so stamping ``model: 'sonnet'`` here would silently run a persona below
    the tier its own definition declares and produce a Sonnet review
    wearing an Opus reviewer's name — cheaper, and invalid. Tier for an
    ``agentType`` call site is the definition's to state, never this
    emitter's to override. Every non-reviewer call in an emitted script
    still carries ``_MODEL_OPT``, so a script composed this way retains
    modeled call sites and reads as partial coverage, not zero, to the
    Workflow model guard.
    """
    phase_call = f"  phase({_js_string_literal(phase_title)});"
    prompt = "Review this plan's completed work."

    if len(reviewers) == 1:
        reviewer = reviewers[0]
        call = (
            "  await agent("
            f"{_js_string_literal(prompt)}, "
            "{ "
            f"label: {_js_string_literal(f'review:{reviewer}')}, "
            f"phase: {_js_string_literal(phase_title)}, "
            f"agentType: {_js_string_literal(reviewer)} "
            "});"
        )
        return f"{phase_call}\n{call}"

    item_calls = ",\n".join(
        "    () => agent("
        f"{_js_string_literal(prompt)}, "
        "{ "
        f"label: {_js_string_literal(f'review:{reviewer}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(reviewer)} "
        "})"
        for reviewer in reviewers
    )
    call = (
        "  await parallel([\n"
        f"{item_calls}\n"
        "  ]);"
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
    review_tier: Optional[str] = None,
    review_roster_fragment: Optional[dict] = None,
) -> str:
    """Compose one Workflow ``.mjs`` script text from already-derived ``waves``.

    Refuses (``NoWavesError``) if ``waves`` is empty — see module docstring
    § Reuse boundary. Every other refusal (``NoWritesDeclaredError``,
    ``NoTestTargetError``) is raised by ``pathspec.commit_pathspec``/
    ``pathspec.terminal_test_scope`` and propagates unchanged: this function
    adds no derivation of its own.

    A review phase (a) composes ONLY when BOTH ``review_tier`` (this repo's
    data — see ``derive_review_tier``) AND ``review_roster_fragment`` (DoE's
    data — see ``_reviewers_for_tier``) are supplied; either alone composes
    no review phase, never a guessed one. A wave over
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
            body_blocks.append(_wave_agent_calls(batch, wave_title))

            batch_pathspec = commit_pathspec(batch)
            commit_title = f"{_commit_phase_title(index)}{suffix}"
            phase_titles.append(commit_title)
            body_blocks.append(_commit_agent_call(batch_pathspec, commit_title, index))

    if review_tier is not None and review_roster_fragment is not None:
        reviewers = _reviewers_for_tier(review_roster_fragment, review_tier)
        phase_titles.append(_REVIEW_PHASE_TITLE)
        body_blocks.append(_review_phase_calls(reviewers, _REVIEW_PHASE_TITLE))

    scope = terminal_test_scope(waves, repo_root=repo_root)
    phase_titles.append(_TEST_PHASE_TITLE)
    body_blocks.append(_test_agent_call(scope, _TEST_PHASE_TITLE))

    meta_block = _meta_block(name, description, phase_titles)
    body = "\n\n".join(body_blocks)

    # Top-level, never `async function run(ctx) { ... }` -- see module
    # docstring § Top-level body, never a defined-but-uninvoked wrapper.
    return f"{meta_block}\n{body}\n"


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

    return compose_script(
        waves,
        name=resolved_name,
        description=resolved_description,
        repo_root=repo_root,
        review_tier=review_tier,
        review_roster_fragment=review_roster_fragment,
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
