"""
coordinator_core.tracker_completion_policy — sat-04 completion-axis tier
policy: pure classification of `code_complete`/`qa_verified` observations
into `tier: "auto"` vs `tier: "suggest"`, plus (C2) the pure builder that
turns a verified revert observation into a `code_complete` retract PAYLOAD,
plus (C4) the one thin emit seam that classifies an assert observation and
appends it via `tracker_transitions.emit_transition`.

Purpose: the policy layer sat-03 deliberately stopped short of —
`tracker_transitions`' own comment at `_REOPEN_TIER` says "tier policy
beyond that binary is sat-04's remit, not this module's", and its module
docstring says the same of tier decisions AND symmetric-retract logic. This
module decides tiering and revert-driven retract-payload construction, and
emits the `code_complete` ASSERT path only; it registers NO op (sat-06's
remit, D2). `detect_symmetric_retract` (C2) builds a payload via
`tracker_transitions.transition_event`; it never calls
`_emit`/`emit_transition` itself — wiring the RETRACT path through the emit
seam is sat-06/sat-07's remit (D4), not this chunk's. `emit_code_complete_
assert` (C4) is this module's only impure function and its only
`repo_root` parameter anywhere in this plan's scope.

Spec backlink: docs/plans/2026-08-18-sat-04-completion-axis-policy.md
§ Key decisions D1/D2/D3/D4, § Tasks C1 (AC1, AC2, AC3, AC9, AC12, AC13,
AC15), C2 (AC4, AC14), C4 (AC8, AC9).

**Trust boundary (D1).** The auto-assert gate named in
`state/roadmap/sovereign-tracker-2026-07-17/clusters.md:41` is a
3-condition rule: real git object + default-branch-reachable +
id-in-commit-trailer binding. This module's classifier is structurally
2-condition (`trailer_bound` and `reachable_on_default_branch is True`).
The third condition — "real git object" — is not dropped; it is asserted
true UPSTREAM by whoever minted `CodeCompleteEvidence`, the same way
`reachable_on_default_branch` itself is a producer-supplied field rather
than something this module computes. This module shells out to git
NOWHERE, per the per-op spawn budget (`coordinator_core :: ipc.py ::
_timeout_for`) and the closed-list shell-out carve-outs doctrine
(`docs/reference/shell-out-carve-outs.md`) — NOT per DR-215, which is the
resident-daemon-retirement / command-type execution-model decision and
does not speak to shelling out inside a single op; an earlier draft of
this plan cited DR-215 for this rule and that citation is retracted (D1).
A later reader tempted to "fix" condition (a) by adding a `git
merge-base --is-ancestor` call here would also find it unrunnable in
practice: `item_project` is 0..n across the fleet and this process has no
guaranteed checkout of the item's repo.

`reachable_on_default_branch` is a tri-state, mirroring
`coordinator_core.ops.emit.resolvers`'s `_stamp_shipped_sha` tri-state
discipline rather than inventing a new one: `True` means checked and
reachable, `False` means checked and NOT reachable, `None` means not
checked (indeterminate). `None` degrades to `"suggest"`, NEVER to a false
`"auto"` and never to a retract — collapsing `None` into `False` would
make the tri-state a lie and would eventually mint retracts on unchecked
evidence (anti-scope). This satisfies Resolution 3's "core functional in
Tier-B with zero `wsc_commit` dependency": Tier-B produces `None` for
every event, so every event classifies `"suggest"`, with no special-cased
branch for it.

Negative-spec:
  - Do NOT import `coordinator_core.ops` from this module. Importing a
    flat `coordinator_core.tracker_*` module before `coordinator_core.ops`
    currently unregisters `tracker.mint_person` via a circular import
    (filed separately, see the plan's Cross-plan coordination section) —
    this module stays import-light so it does not widen that blast
    radius.
  - Do NOT coerce `reachable_on_default_branch is None` to `False`.
  - Do NOT add a `subprocess`/`git` call, a network call, or a
    `tracker_store` write-path import to this module (AC9).
  - Do NOT wire an "auto" path for `classify_qa_verified` — see its own
    docstring; no evidence shape today is strong enough to auto-assert
    that axis.
  - Do NOT invent an `idempotency_key` parameter anywhere in this module
    (an earlier review sidecar proposed one on the write path; no such
    parameter and no such function exists — see the plan's Problem/D3).
    `detect_symmetric_retract` addresses its payload the same way every
    other transition payload does: through `tracker_transitions`' own
    internal addressing, never a caller-supplied key.
  - Do NOT call `tracker_transitions.emit_transition` or `_emit` from
    `detect_symmetric_retract`, and do NOT give it a `repo_root` parameter
    — it builds a payload only (D4); emitting the revert-driven retract is
    out of this plan's scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from coordinator_core import tracker_transitions
from coordinator_core.tracker_transitions import _SUGGEST_TIER

_AUTO_TIER: Literal["auto"] = "auto"
_RETRACTED_TO_STATE = "retracted"
_ASSERTED_STATE = "asserted"

_CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST: frozenset[str] = frozenset(
    {"verify-with-effort", "human-attested"}
)
"""(C6) `closure_fidelity` values that force `"suggest"` regardless of what
`evidence` otherwise satisfies — the ONE degradation branch every caller of
`classify_code_complete_tier` shares (threaded through by the module's two
in-module callers, `emit_code_complete_assert` and
`detect_symmetric_retract`, per this chunk's dispatch brief).

`"human-attested"` was added after C6 shipped, on an adversarial read of the
delivered code. C6's own remit was the `verify-with-effort` guarantee alone and
it correctly declined to infer this, so the set held one member and an item
whose classification says a HUMAN must attest it still auto-asserted on a purely
machine-observed signal (trailer_bound + reachable). That is a false auto in the
one axis built to prevent false autos, so it is a defect rather than a scope
extension: a tier named for human attestation cannot be discharged by a machine
observation. DR-closure-fidelity-tier-axis's D1-D4 never ruled on this tier,
which is why it was available to get wrong.

`"auto-observable"` items pass through unaffected: this set names what
DEGRADES, not the closed vocabulary of `closure_fidelity` itself, which this
module never validates (that is the caller's resolved-projection concern,
per the module docstring's import-light negative-spec)."""


@dataclass(frozen=True)
class CodeCompleteEvidence:
    """Structured evidence for a `code_complete` observation.

    `trailer_bound` (AC15): pinned as a case-sensitive EXACT match of
    `item.id` against the commit's `Closes:` trailer value. This binding
    comparison is computed UPSTREAM of this module (D1) — the producer of
    a `CodeCompleteEvidence` instance is the one that ran the match; this
    dataclass only carries its result. This is an assumption about
    sat-02's `item.id` shape, taken as given because sat-02 has not landed
    a different one — flagged here as the assumption to revisit if sat-02
    ever lands a different `item.id` shape.

    `reachable_on_default_branch` is a tri-state (`bool | None`): `None`
    means "not checked" and must never be coerced to `False` — see the
    module docstring's trust-boundary section.
    """

    sha: str
    trailer_bound: bool
    reachable_on_default_branch: bool | None


def classify_code_complete_tier(
    evidence: CodeCompleteEvidence,
    *,
    closure_fidelity: str,
) -> str:
    """Classify a `code_complete` observation's tier (AC1, AC2).

    `closure_fidelity` is REQUIRED and keyword-only, no default (C6) —
    this module cannot resolve an item's `closure_fidelity` itself (its
    own negative-spec forbids the `coordinator_core.ops`/`tracker_store`
    imports that would let it read the item's projected state), so
    resolving it is explicitly the CALLER's job; a default of `None` or
    `"auto-observable"` is exactly how the "`verify-with-effort` never
    auto-asserts" guarantee would silently lapse for any caller that
    forgets to pass it, so no default exists.

    Returns `"suggest"` immediately if `closure_fidelity` is a member of
    `_CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST` — checked BEFORE the evidence
    predicate below, so a `verify-with-effort` item can never reach
    `"auto"` no matter how strong its `evidence` is. Otherwise returns
    `"auto"` iff `evidence.trailer_bound` is true AND
    `evidence.reachable_on_default_branch is True` — an explicit identity
    check against `True`, not a truthy check, so that `None` (indeterminate)
    never satisfies the condition. Returns `"suggest"` for every other
    input, including `reachable_on_default_branch is None`. Never raises.

    The return annotation is `str`, not a two-value `Literal`, because
    `tracker_transitions.TRANSITION_TIERS` (this module's SSOT for the tier
    vocabulary) is a 4-value closed set (`auto`/`suggest`/`direct`/
    `deferred`, C2/C1) — this function still only ever produces `"auto"` or
    `"suggest"` (unchanged by this widening; see the two branches above),
    it is only the TYPE that no longer over-asserts a two-value universe.
    """
    if closure_fidelity in _CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST:
        return _SUGGEST_TIER
    if evidence.trailer_bound and evidence.reachable_on_default_branch is True:
        return _AUTO_TIER
    return _SUGGEST_TIER


@dataclass(frozen=True)
class QaVerifiedEvidence:
    """Structured evidence for a `qa_verified` observation.

    Carries the real `qa_verified` evidence shape per the roadmap research
    corpus — deliberately not a bare `object` — even though
    `classify_qa_verified` ignores every field today (AC3). The type IS
    the negative-spec surface: a strongly-typed, non-trivial parameter is
    what stops a future editor from quietly wiring an auto path onto this
    axis, since any such change would have to first give this dataclass
    fields worth branching on and then still contend with the pinned
    docstring below.

    `confidence` is named here, not consumed: `evidence.confidence`
    surfacing for `qa_verified` is deferred to sat-06 by the research
    corpus (see the plan's Out of scope) — recorded so a reader of this
    module alone does not assume confidence-scoring lives here.
    """

    source: str
    confidence: float | None = None


def classify_qa_verified(evidence: QaVerifiedEvidence) -> str:
    """Always `"suggest"` — no auto path exists for this axis (AC3).

    This is a PINNED CONTRACT, not an unimplemented stub: `qa_verified`
    has no evidence shape strong enough to auto-assert against, per the
    roadmap research corpus. Do not "simplify" this into returning a bare
    constant without the typed `evidence` parameter — the typed parameter
    and this docstring are the negative-spec that keeps a future editor
    from wiring an auto path in. Confirmed even against a maximally-strong
    evidence payload (AC3's test).

    The return annotation is `str`, not `Literal["suggest"]` (C2) — same
    widening rationale as `classify_code_complete_tier`: this function's
    OWN behavior is unchanged and stays `"suggest"`-only, per this
    docstring's pinned contract above; only the type no longer hardcodes a
    narrower vocabulary than `tracker_transitions.TRANSITION_TIERS` (C1's
    SSOT) actually has. This function still cannot produce `"queued"`
    (`tracker_transitions`' `"deferred"` spelling) or `"direct"` — those
    describe a human's deferred assertion and a reopen cascade
    respectively, not machine evidence classification (see this chunk's
    dispatch brief); it stays `"suggest"`-only.
    """
    return _SUGGEST_TIER


def detect_symmetric_retract(
    item_id: str,
    revert_evidence: CodeCompleteEvidence,
    *,
    actor: str,
    source_observation_id: str,
    closure_fidelity: str,
) -> dict | None:
    """Build a `code_complete` retract PAYLOAD from a verified revert
    observation (C2, D3, D4) — pure, no disk access, no `repo_root`. Mirrors
    the pure/emit split sat-03 draws between `tracker_transitions.
    transition_event` (payload constructor) and its `_emit` writer: this
    function returns a payload only; it never calls `_emit` or
    `emit_transition` itself. Wiring this payload through `emit_transition`
    is out of scope for this plan (D4) — it lands with sat-06/sat-07's
    ingest, which is the caller that will eventually own `repo_root` and the
    decision of WHEN to call this function.

    `closure_fidelity` is REQUIRED and keyword-only, threaded straight into
    the internal `classify_code_complete_tier` call below (C6) — the SAME
    shared classifier and the SAME `_CLOSURE_FIDELITY_DEGRADES_TO_SUGGEST`
    gate `emit_code_complete_assert` uses, not a parallel branch. This is a
    DELIBERATE, tested consequence (staff-eng Finding 3), not a silent
    side-effect: a `verify-with-effort` revert's `revert_tier` degrades to
    `"suggest"` under this gate the same way an assert would, so it fails
    the `revert_tier != _AUTO_TIER` check below and this function returns
    `None` — retract detection for that item is suppressed, not merely its
    assert path. This function does NOT carve out a separate "retract-only"
    posture that would let a `verify-with-effort` revert retract even though
    a `verify-with-effort` assert cannot; reusing one shared classifier for
    both callers, per this chunk's brief, means both degrade together. See
    `test_c6_*` in the test module for the explicit regression guard over
    this consequence.

    *revert_evidence* describes the REVERT commit itself, not the completing
    commit it undoes: `revert_evidence.sha` is the revert's own sha, and
    `revert_evidence.trailer_bound` / `revert_evidence.reachable_on_default_
    branch` describe whether the revert commit is itself bound to *item_id*
    and reachable on the default branch. Returns `None` — never a payload —
    unless the revert evidence clears the SAME gate `classify_code_complete_
    tier` applies to an assert (`trailer_bound` true AND `reachable_on_
    default_branch is True`, reusing that classifier rather than
    duplicating its predicate): an unverified revert never retracts (AC4).
    In particular `reachable_on_default_branch is None` (not checked) never
    downgrades to a retract, same tri-state discipline as the assert path
    (D1) — it degrades to "do nothing" here rather than to `"suggest"`,
    since there is no `tier: "suggest"` retract concept in this plan; a
    revert that cannot be verified simply does not retract yet.

    The returned payload carries `evidence={"sha": revert_evidence.sha}` —
    the REVERT's own sha, NEVER the completing sha it undoes — plus
    *source_observation_id*, which the caller must supply as the revert
    observation's OWN id (D3), not the id of the observation that produced
    the original `code_complete` assert. Addressing a retract on the
    revert's own identity (rather than on the completing sha it reverts) is
    what keeps `assert(A)` and `retract(R)` from ever colliding in
    `tracker_transitions._mint_address` / `_dedup_check_address` — the
    `to_state` differs (`"asserted"` vs `"retracted"`) and the `evidence.sha`
    differs (the completing sha vs the revert's sha). The residual
    collision this leaves open — a later revert-of-the-revert re-asserting
    the ORIGINAL completing sha — is not this function's to close; that is
    C3's `generation` fix in `tracker_transitions.py` (D8), applied
    independently of what this function returns.

    Built via `tracker_transitions.transition_event` (never a hand-rolled
    dict) so the payload shape stays the single source of truth
    `tracker_transitions` owns: `axis="code_complete"`,
    `to_state="retracted"`, `from_state="asserted"` (a revert-driven retract
    only ever fires against a previously-asserted `code_complete`, unlike
    `_build_reopen_cascade`'s direct-human retracts which are conditioned on
    the CURRENT projected state instead — see that function's docstring for
    the sibling shape), `tier="auto"` (this function's own gate already
    required `reachable_on_default_branch is True` to reach this point, so
    the retract it produces is never `"suggest"`-tier), and
    `source_observation_id` populated — deliberately NOT the `None`-shaped
    direct-human branch `_build_reopen_cascade` uses, since a revert-driven
    retract IS a re-observation with a stable identity, and taking the
    `None` branch would route it onto `_mint_address`'s nonce fallback,
    making every re-detection of the SAME revert mint a distinct id (the
    exact failure AC14 exists to rule out).

    **Rollback / migration note.** A wrongly-`auto`-asserted `code_complete`
    event is corrected by a COUNTERMANDING event on the `manual_close` axis
    (via `reopen_cascade`), never by mutating the mis-tiered event in place
    — the transition-event log this function's payload eventually feeds is
    append-only (inherited from sat-03, not reopened by this plan). Minted
    event ids are stable once written: a pre-existing event's `id` never
    changes and is never rewritten. Pre-existing events ARE, however,
    RE-ADDRESSED at read time — `tracker_transitions._find_existing_by_
    address` recomputes `_dedup_check_address` over every stored event using
    the POST-C3 function, never by comparing stored ids, so a post-C3
    re-observation of a pre-C3 observation is matched (or missed) under the
    new key. Under C3's generation design this is symmetric: `generation` is
    read back from each event's own stamped field (absent reads as 0, AC7a),
    so both the stored side and the incoming side of the comparison derive
    the same way. That is strictly better than the rejected `from_state`
    design (D8), where the field was present on stored events but absent
    from every caller that never passed it — this function never passes a
    `generation` at all, leaving that entirely to `_emit`/`_emit_batch`'s
    stamping, exactly as `transition_event`'s own closed field set requires.
    """
    revert_tier = classify_code_complete_tier(
        revert_evidence, closure_fidelity=closure_fidelity
    )
    if revert_tier != _AUTO_TIER:
        return None

    return tracker_transitions.transition_event(
        item_id,
        "code_complete",
        _RETRACTED_TO_STATE,
        from_state=_ASSERTED_STATE,
        actor=actor,
        evidence={"sha": revert_evidence.sha},
        tier=_AUTO_TIER,
        source_observation_id=source_observation_id,
    )


def emit_code_complete_assert(
    item_id: str,
    evidence: CodeCompleteEvidence,
    *,
    actor: str,
    source_observation_id: str | None = None,
    repo_root: Path,
    closure_fidelity: str,
) -> dict:
    """Classify a `code_complete` observation (`classify_code_complete_tier`)
    and emit its ASSERT through `tracker_transitions.emit_transition` (C4,
    D4) — the ONE seam in this plan's scope that touches `repo_root` or
    performs any I/O; every other function in this module is pure.

    `closure_fidelity` is REQUIRED and keyword-only (C6), threaded straight
    into `classify_code_complete_tier` — resolving an item's
    `closure_fidelity` from its projected state is the CALLER's job, not
    this module's (see the module docstring's import-light negative-spec
    and `classify_code_complete_tier`'s own docstring).

    Covers the `code_complete` ASSERT path only. C2's
    `detect_symmetric_retract` builds a retract PAYLOAD but this function
    never calls it and never emits one: wiring the revert-driven retract
    through `emit_transition` is out of scope for this plan (D4) — it lands
    with sat-06/sat-07's ingest, same as the assert-triggering observation
    itself (a caller deciding WHEN a `code_complete` observation arrives).

    `evidence.sha` becomes `evidence={"sha": evidence.sha}` on the emitted
    payload — the completing commit's own sha, mirroring
    `detect_symmetric_retract`'s `evidence["sha"]` shape for the retract
    side. `tier` is never accepted as a caller-supplied argument: it is
    always the classifier's own output, so a caller cannot bypass
    `classify_code_complete_tier` by passing a tier directly.

    `applied_at` (AC8) is populated at creation for `tier="auto"` and left
    `null` for `tier="suggest"` by `tracker_transitions._emit` — that
    mechanism is sat-03's, already shipped, and is NOT reimplemented here;
    see `test_ac8_*` in the test module for the regression guard over it.
    """
    tier = classify_code_complete_tier(evidence, closure_fidelity=closure_fidelity)
    return tracker_transitions.emit_transition(
        item_id,
        "code_complete",
        _ASSERTED_STATE,
        actor=actor,
        evidence={"sha": evidence.sha},
        tier=tier,
        source_observation_id=source_observation_id,
        repo_root=repo_root,
    )
