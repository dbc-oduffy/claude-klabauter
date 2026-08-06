"""
coordinator_core.ops.ceremony.node_handlers — Handler library for D/J/F/B/X node types.

Purpose: The shared handler library consumed by branch_resolution.py (the surviving
engine of the retired ceremony.wsc_resolve op). Each handler produces a schema-valid
node-ledger entry (using receipt_schema factory helpers) for its respective node
class.  This is a PURE LIBRARY module — no op registration, no disk I/O, no
@register_op call.

Handler contract:
  D handler — handle_d(step_id, resolving_op, evidence, *, tail_step=False) → D-node dict
  J handler — emit_j(step_id, answer="") → J-node dict with the canonical question text
  F handler — emit_f(step_id, filled="") → F-node dict with the canonical slot description
  B handler — emit_b(step_id, pre_resolved_evidence, em_adjudication) → B-node dict

Classification helper:
  classify_step(step_id) → node-type string (D/J/F/B/X) or None if unknown

Bulk-eligibility helper (C2):
  classify_bulk_eligibility(kind, in_reply_to, target_resolution) → (eligible, reason)
  — a PURE classifier (no disk I/O) over already-resolved inputs; the disk-backed
  in_reply_to target resolution lives in branch_resolution.py's
  _resolve_in_reply_to_target, which stays outside this module's no-disk-I/O
  contract by construction.

The 8 J-questions and 3 F-slots are defined as module-level dicts keyed by step_id
(authoritative — changing a question/slot here changes all consumers).

Spec backlink:
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § C2.3 / A3
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.node-map.md
  docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md § C2 (STEP_2_65B widened
    contract, classify_bulk_eligibility added)
"""

from __future__ import annotations

from typing import Any, Optional

from coordinator_core.ops.ceremony.receipt_schema import (
    VALID_NODE_TYPES,
    make_b_node,
    make_d_node,
    make_f_node,
    make_j_node,
    make_x_node,
)

# ---------------------------------------------------------------------------
# Step-ID constants — canonical keys for the wsc node map
# ---------------------------------------------------------------------------
# Step IDs mirror the wsc SKILL step numbering; used as dict keys and node IDs
# throughout the ceremony pipeline.

STEP_0 = "step_0"
STEP_1A = "step_1a"
STEP_1B = "step_1b"
STEP_1C = "step_1c"
STEP_1_2 = "step_1.2"
STEP_2A = "step_2a"
STEP_2B = "step_2b"
STEP_2_4A = "step_2.4-a"
STEP_2_4B = "step_2.4-b"
STEP_2_6_1 = "step_2.6.1"
STEP_2_6_2 = "step_2.6.2"
STEP_2_6_3 = "step_2.6.3"
STEP_2_6_3A = "step_2.6.3a"
STEP_2_6_4 = "step_2.6.4"
STEP_2_6_5 = "step_2.6.5"
STEP_2_6_5A = "step_2.6.5a"
STEP_2_6_6A = "step_2.6.6a"
STEP_2_6_6B = "step_2.6.6b"
STEP_2_6_6C = "step_2.6.6c"
STEP_2_6_7 = "step_2.6.7"
STEP_2_6_8 = "step_2.6.8"
STEP_2_65A = "step_2.65a"
STEP_2_65B = "step_2.65b"
STEP_2_65C = "step_2.65c"
STEP_2_67A = "step_2.67a"
STEP_2_67B = "step_2.67b"
STEP_2_7 = "step_2.7"
STEP_2_75 = "step_2.75"
STEP_2_8A = "step_2.8a"
STEP_2_8B = "step_2.8b"
STEP_2_8C = "step_2.8c"
STEP_2_9A = "step_2.9a"
STEP_2_9B = "step_2.9b"
STEP_B1 = "step_B1"
STEP_2_9C = "step_2.9c"
STEP_2_9D = "step_2.9d"
STEP_2_9E = "step_2.9e"
STEP_2_9_OBS = "step_2.9-obs"
STEP_2_95A = "step_2.95a"
STEP_2_95B = "step_2.95b"
STEP_2_96 = "step_2.96"
STEP_3_0 = "step_3.0"
STEP_3_1 = "step_3.1"
STEP_3_2 = "step_3.2"
STEP_3_3 = "step_3.3"
STEP_3_5A = "step_3.5a"
STEP_3_5B = "step_3.5b"
STEP_4A = "step_4a"
STEP_4B = "step_4b"

# ---------------------------------------------------------------------------
# Node-type classification — canonical map for the wsc ceremony
# ---------------------------------------------------------------------------
# Source: node-map § Step-by-Step Node-Type Table + Count Summary (corrected).
# 37 D / 8 J / 3 F / 1 B / 0 X = 49 pipeline steps.
#
# Negative-spec: step_4c is documented in the node-map table as a J-labelled
# meta-step that describes the EM's flag-severity-triage practice (CLAUDE.md
# § Flag Severity), but it is NOT counted in the 49 pipeline steps and is NOT
# emitted as a pipeline node.  Do NOT add it here.

_STEP_NODE_TYPES: dict[str, str] = {
    # -------- D — deterministic-with-receipt --------
    STEP_0:       "D",
    STEP_1C:      "D",
    STEP_2A:      "D",
    STEP_2_4A:    "D",
    STEP_2_6_1:   "D",
    STEP_2_6_2:   "D",
    STEP_2_6_3A:  "D",
    STEP_2_6_4:   "D",
    STEP_2_6_5:   "D",
    STEP_2_6_5A:  "D",
    STEP_2_6_6A:  "D",
    STEP_2_6_6B:  "D",
    STEP_2_6_8:   "D",
    STEP_2_65A:   "D",
    STEP_2_65C:   "D",
    STEP_2_7:     "D",
    STEP_2_75:    "D",
    STEP_2_8B:    "D",
    STEP_2_9A:    "D",
    STEP_2_9B:    "D",  # reclassified from J in Scout A; now D-evidence feeding B1
    STEP_2_9C:    "D",
    STEP_2_9D:    "D",
    STEP_2_9E:    "D",
    STEP_2_9_OBS: "D",
    STEP_2_95A:   "D",
    STEP_3_0:     "D",
    STEP_3_1:     "D",
    STEP_3_2:     "D",
    STEP_3_3:     "D",
    STEP_3_5A:    "D",
    STEP_3_5B:    "D",
    STEP_4A:      "D",
    STEP_2_6_3:   "D",  # reclassified X→D: started_at + git log --diff-filter=A (C1)
    STEP_2_96:    "D",  # reclassified X→D: completeness-checklist mirror read (C3)
    STEP_2_67A:   "D",  # reclassified X→D: filesystem-mtime scratch scan (C2 spinoff)
    STEP_1B:      "D",  # reclassified F→D: lesson authored disk-first via coordinator-lesson-add; op records provenance, not payload (Option B, memo 2026-07-08)
    STEP_2_4B:    "D",  # reclassified F→D: plan-reconciliation ALLOWLIST edit written in place at Step 2.4; op records provenance, not payload (Option B, memo 2026-07-08)
    # -------- J — judgment-elicited (8) --------
    STEP_1A:      "J",
    STEP_1_2:     "J",
    STEP_2_6_7:   "J",
    STEP_2_65B:   "J",
    STEP_2_67B:   "J",
    STEP_2_8A:    "J",
    STEP_2_8C:    "J",
    STEP_2_95B:   "J",
    # -------- F — free-authored prose (3) --------
    STEP_2B:      "F",
    STEP_2_6_6C:  "F",
    STEP_4B:      "F",
    # -------- X — illegible-state gap (0 — reserved; BranchResolution/receipts may still emit X) --------
    # -------- B — EM-turn bracket (1) --------
    STEP_B1:      "B",
}

# ---------------------------------------------------------------------------
# J-question corpus (8 discriminating questions)
# ---------------------------------------------------------------------------
# Authoritative text sourced from node-map § Step-by-Step Node-Type Table.
# answer="" until the EM fills the J-node during the EM turn.
#
# These are 1-2-choice prompts — the EM answers yes/no or supplies the bounded
# set.  The handler DOES NOT auto-answer (anti-scope: J nodes must not be
# pre-resolved; pre-resolving a J step re-introduces hallucination risk).

J_QUESTIONS: dict[str, str] = {
    STEP_1A: (
        "Is this a generalizable rule (correction / surprising behavior / failed pattern),"
        " not a one-off fix or pipeline-run detail?"
    ),
    STEP_1_2: (
        "Would this apply to any project type using the coordinator pipeline?"
    ),
    STEP_2_6_7: (
        "Did this session produce commits beyond doc/lesson housekeeping?"
        " Group related commits; skip trivial typo/format commits."
    ),
    STEP_2_65B: (
        "For each open memo, name its disposition: a decision"
        " (accepted|partial|declined, with realized_by for accepted/partial) or an"
        " actioned_note (consult/fyi reply). A kind: fyi memo whose in_reply_to"
        " (if any) does not point at a still-open ask may instead take the cheap"
        " bulk no-action-needed disposition — see"
        " classify_bulk_eligibility below; anything else needs an individual"
        " disposition. A memo you don't name here stays open."
        " ANSWER SHAPE: a JSON array of objects, one per named memo, e.g."
        ' [{"memo": "<memo>.md", "decision": "accepted",'
        ' "realized_by": "<sha>"}, {"memo": "<memo>.md",'
        ' "actioned_note": "<text>"}]. The array is decoded as literal JSON;'
        " prose here surfaces as a malformed answer, not as zero memos."
    ),
    STEP_2_67B: (
        "Is this file still load-bearing for active work"
        " (PM needs it next turn / sibling workstream / active plan citation),"
        " or is it trash?"
        " Default = delete; keep requires explicit one-line rationale."
    ),
    STEP_2_8A: (
        "Did this session surface a transient gotcha, critical blocker, or env-specific"
        " caveat that would otherwise be lost before the next session start?"
        " (One-slot escape valve — second write overwrites.)"
    ),
    STEP_2_8C: (
        "Which tracked items / action-items / README rows did this session's work"
        " complete or progress?"
        " Only touch rows this session affected."
    ),
    STEP_2_95B: (
        "Anything cross-cutting that line-level review (Step 2.9) wouldn't surface"
        " — install-surface regeneratability, convention contact-points, secret surface,"
        " stale wiki refs?"
        " Tradeoff-free corrections fold in; real tradeoffs → PM."
    ),
}

# ---------------------------------------------------------------------------
# F-slot corpus (3 authoring slots)
# ---------------------------------------------------------------------------
# Slot descriptions sourced from node-map § Step-by-Step Node-Type Table.
# filled="" until the EM authors the prose during the EM turn.
#
# These are irreducible EM prose slots — no bounded-choice framing applies.
# The handler DOES NOT author prose (anti-scope).

F_SLOTS: dict[str, str] = {
    STEP_2B: (
        "Plan completion notes — check off completed tasks; write what was built,"
        " key decisions, and notable outcomes."
        " Requires session memory; plan doc is updated in place."
    ),
    STEP_2_6_6C: (
        "Completion entry body paragraph."
        " ONE paragraph ≤8 sentences: what shipped + why it matters."
        " Irreducible EM prose from session context."
    ),
    STEP_4B: (
        "\"Work done\" narrative synthesis."
        " 1-2 sentence summary of what shipped and why it matters."
        " Irreducible EM synthesis from session context."
    ),
}

# ---------------------------------------------------------------------------
# X-step missing-signal descriptions
# ---------------------------------------------------------------------------
# Authoritative text sourced from node-map § X-step contract-gap index.

X_MISSING_SIGNALS: dict[str, str] = {
    # X is currently unpopulated: all prior X-steps reclassified to D (C1/C2/C3 spinoffs).
    # BranchResolution and receipt consumers may still emit X nodes at runtime; this dict
    # governs only the step-registry entries (wsc_resolve pipeline steps).
}

# ---------------------------------------------------------------------------
# STEP_2_65B bulk-eligibility classifier (C2)
# ---------------------------------------------------------------------------
# Spec backlink: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md
#   § C2 / AC4a — PM ruling (2026-07-26) on the bulk no-action-needed fast path.


#: `_resolve_in_reply_to_target` (branch_resolution.py, disk-backed) returns one of
#: these three strings, or the caller passes `None` when `in_reply_to` itself
#: is absent (not applicable — no resolution was ever attempted).
TargetResolution = Optional[str]  # "open" | "closed" | "unresolvable" | None


def classify_bulk_eligibility(
    kind: str, in_reply_to: Optional[str], target_resolution: TargetResolution,
) -> tuple[bool, str]:
    """Classify whether an inbound memo may take STEP_2_65B's cheap bulk
    "no-action-needed" disposition, per the PM-ruled five cases (AC4a):

      1. not ``kind: fyi``                                        -> NOT eligible
      2. ``kind: fyi``, no ``in_reply_to`` at all                  -> eligible
      3. ``kind: fyi``, ``in_reply_to`` resolves to a CLOSED target -> eligible
      4. ``kind: fyi``, ``in_reply_to`` resolves to an OPEN target  -> NOT eligible
      5. ``kind: fyi``, ``in_reply_to`` UNRESOLVABLE (typo/missing) -> NOT eligible,
         fail CLOSED — an ambiguous reference never reads as eligible.

    ``kind: fyi`` is the LOAD-BEARING filter; ``in_reply_to`` is a narrow
    secondary fail-closed check, not a second independent gate (PM ruling
    2026-07-26 — `in_reply_to` landed write-side only 2026-07-25 and is
    present on ~8 of 335 memos in the corpus today, so on its own it barely
    discriminates; its job here is solely to catch the still-open-ask case
    and to fail closed on an unresolvable reference).

    This function is a PURE classifier over already-resolved inputs — it does
    NOT read disk itself (module-level no-disk-I/O contract, see module
    docstring). Callers resolve ``target_resolution`` on disk first (see
    ``wsc_resolve._resolve_in_reply_to_target``) and pass the result in.

    Returns:
        (eligible, reason) — ``reason`` is a human-readable string suitable
        for surfacing in a resolve-refusal error or as evidence.

    Negative-spec: does NOT treat an unresolvable ``in_reply_to`` as eligible
    and does NOT treat a still-open target as eligible — both fail CLOSED to
    "individual disposition required", never open.
    """
    if kind != "fyi":
        return False, "not kind: fyi — individual disposition required"
    if in_reply_to is None:
        return True, "kind: fyi, no in_reply_to — bulk-eligible"
    if target_resolution == "closed":
        return True, (
            f"kind: fyi, in_reply_to {in_reply_to!r} resolves to a closed target — bulk-eligible"
        )
    if target_resolution == "open":
        return False, (
            f"kind: fyi, in_reply_to {in_reply_to!r} resolves to a still-open target — "
            "individual disposition required"
        )
    return False, (
        f"kind: fyi, in_reply_to {in_reply_to!r} is unresolvable — fail CLOSED, "
        "individual disposition required"
    )


# ---------------------------------------------------------------------------
# D handler
# ---------------------------------------------------------------------------


def handle_d(
    step_id: str,
    resolving_op: str = "",
    evidence: dict[str, Any] | None = None,
    *,
    tail_step: bool = False,
) -> dict[str, Any]:
    """Produce a D-node dict for the given step.

    Args:
        step_id:      canonical step ID (e.g. STEP_2_6_1).
        resolving_op: name of the script/op that resolved this branch
                      (e.g. "git log --diff-filter=A", "cs_claim_plan").
        evidence:     dict of read provenance — {method, path, value, ...}.
                      For tail D-nodes: should carry fleet-op result shape
                      {acted:[], skipped:[], failed:[]} so compute_op_tail can aggregate.
        tail_step:    True for the 8 deterministic-tail fleet-ops (used to
                      derive op_tail in receipt_schema.compute_op_tail).

    Returns:
        Schema-valid D-node dict (receipt_schema.make_d_node shape).
    """
    return make_d_node(
        node_id=step_id,
        resolving_op=resolving_op,
        evidence=evidence,
        tail_step=tail_step,
    )


# ---------------------------------------------------------------------------
# J handler
# ---------------------------------------------------------------------------


def emit_j(step_id: str, answer: str = "") -> dict[str, Any]:
    """Produce a J-node dict with the canonical discriminating question for step_id.

    The question text is sourced from J_QUESTIONS (authoritative corpus).
    Raises KeyError if step_id is not one of the 8 J-steps.

    Anti-scope: does NOT auto-answer.  answer="" until the EM fills the node
    during the EM turn.  Pre-resolving a J step re-introduces hallucination risk
    (the ceremony guards exist because the pipeline cannot reliably determine
    which memos were resolved, whether a lesson is universal, etc.).

    Args:
        step_id: canonical step ID; must be a key in J_QUESTIONS.
        answer:  EM-supplied answer (empty string in phase-1 receipt).

    Returns:
        Schema-valid J-node dict (receipt_schema.make_j_node shape).

    Raises:
        KeyError: if step_id is not one of the 8 registered J-steps.
    """
    question = J_QUESTIONS[step_id]  # intentional KeyError if not a J-step
    return make_j_node(node_id=step_id, question=question, answer=answer)


# ---------------------------------------------------------------------------
# F handler
# ---------------------------------------------------------------------------


def emit_f(step_id: str, filled: str = "") -> dict[str, Any]:
    """Produce an F-node dict with the canonical slot description for step_id.

    The slot description is sourced from F_SLOTS (authoritative corpus).
    Raises KeyError if step_id is not one of the 3 F-steps.

    Anti-scope: does NOT author prose.  filled="" until the EM authors the prose
    during the EM turn.  The 3 F-steps are irreducible EM writing —
    completion narrative, plan completion notes, etc.

    Args:
        step_id: canonical step ID; must be a key in F_SLOTS.
        filled:  EM-authored prose (empty string in phase-1 receipt).

    Returns:
        Schema-valid F-node dict (receipt_schema.make_f_node shape).

    Raises:
        KeyError: if step_id is not one of the 3 registered F-steps.
    """
    slot = F_SLOTS[step_id]  # intentional KeyError if not an F-step
    return make_f_node(node_id=step_id, slot=slot, filled=filled)


# ---------------------------------------------------------------------------
# B handler
# ---------------------------------------------------------------------------


def emit_b(
    step_id: str,
    pre_resolved_evidence: dict[str, Any] | None = None,
    em_adjudication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a B-node dict with the generic two-slot shape.

    GENERIC — the slot names (pre_resolved_evidence / em_adjudication) are
    ceremony-independent.  For wsc B1 (the post-implementation code-review wave):
      pre_resolved_evidence carries the review-wave dispatch-plan keys:
        {slice_count, partition_boundaries, docs_checker_inclusion} — pre-resolved
        as D-evidence by wsc_resolve from steps 2.9a + 2.9b + 2.9e.
      em_adjudication carries the EM-filled review verdict after the EM runs the
        wave: {aggregate_verdict: "WARN"|"BLOCKED", integration_disposition: ...}.

    A different ceremony fills different blob content WITHOUT renaming these keys.
    The tell that the schema was built wrong: a second ceremony needs a rename.

    Anti-scope: do NOT add wsc-specific keys (dispatch_plan, adjudication,
    review_verdict) here — those are instance data in the blobs, not schema keys.

    Args:
        step_id:               canonical step ID (e.g. STEP_B1).
        pre_resolved_evidence: D-evidence blob from the resolve phase; {} if not
                               yet available (e.g. when scaffolding the receipt
                               before wsc_resolve fills in the review-wave plan).
        em_adjudication:       EM-filled verdict blob; {} in the phase-1 receipt
                               (filled by the EM during the B-bracket turn).

    Returns:
        Schema-valid B-node dict (receipt_schema.make_b_node shape).
    """
    return make_b_node(
        node_id=step_id,
        pre_resolved_evidence=pre_resolved_evidence,
        em_adjudication=em_adjudication,
    )


# ---------------------------------------------------------------------------
# X handler (convenience; wsc_resolve builds X-nodes for contract-gap steps)
# ---------------------------------------------------------------------------


def emit_x(step_id: str) -> dict[str, Any]:
    """Produce an X-node dict with the canonical missing-signal description.

    The description is sourced from X_MISSING_SIGNALS.  Raises KeyError if
    step_id is not one of the registered X-steps (X_MISSING_SIGNALS keys).

    X-nodes are NOT auto-resolvable; they represent contract-gap steps that are
    blocked by missing on-disk signals (SESSION_START_TIME, Tasks-API mirror).
    They appear in the receipt as documentation — the EM sees the gap.

    Args:
        step_id: canonical step ID; must be a key in X_MISSING_SIGNALS.

    Returns:
        Schema-valid X-node dict (receipt_schema.make_x_node shape).

    Raises:
        KeyError: if step_id is not one of the registered X-steps (X_MISSING_SIGNALS keys).

    Review: code-reviewer — count-free phrasing; 2.6.3, 2.96, and 2.67A reclassified X→D
        2026-07-06 (C1/C2/C3 spinoffs). X_MISSING_SIGNALS is now empty; emit_x raises KeyError
        for all step IDs until a new X-step is registered.
    """
    missing_signal = X_MISSING_SIGNALS[step_id]  # intentional KeyError if not X
    return make_x_node(node_id=step_id, missing_signal=missing_signal)


# ---------------------------------------------------------------------------
# Classification helper
# ---------------------------------------------------------------------------


def classify_step(step_id: str) -> str | None:
    """Return the node type (D/J/F/B/X) for the given step ID, or None if unknown.

    The authoritative classification map is _STEP_NODE_TYPES.  Consumers that
    need to dispatch to the correct handler can use this before calling handle_d /
    emit_j / emit_f / emit_b / emit_x.

    Args:
        step_id: canonical step ID string (e.g. STEP_1A, "step_1a").

    Returns:
        One of "D", "J", "F", "B", "X" if the step is known; None otherwise.
    """
    return _STEP_NODE_TYPES.get(step_id)


def known_j_step_ids() -> list[str]:
    """Return the ordered list of the 8 canonical J-step IDs."""
    return [s for s, t in _STEP_NODE_TYPES.items() if t == "J"]


def known_f_step_ids() -> list[str]:
    """Return the ordered list of the 3 canonical F-step IDs."""
    return [s for s, t in _STEP_NODE_TYPES.items() if t == "F"]


def known_b_step_ids() -> list[str]:
    """Return the list of canonical B-step IDs (currently 1: step_B1)."""
    return [s for s, t in _STEP_NODE_TYPES.items() if t == "B"]
