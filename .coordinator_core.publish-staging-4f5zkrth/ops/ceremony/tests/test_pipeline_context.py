"""
coordinator_core.ops.ceremony.tests.test_pipeline_context

Tests for the ceremony resolved-state data model (PipelineContext + BranchResolution).

Coverage:
  (a) branch_resolution_construction   — BranchResolution accepts valid node_type; rejects invalid
  (b) branch_resolution_round_trip     — to_dict / from_dict is lossless
  (c) pipeline_context_construction    — PipelineContext constructs with defaults
  (d) pipeline_context_add_branch      — add_branch / get_branch accessors work
  (e) pipeline_context_add_node        — add_node / get_node accessors work; node uses receipt_schema shape
  (f) pipeline_context_round_trip      — to_dict / from_dict is lossless (nodes + branches + disposition)
  (g) pipeline_context_round_trip_empty — empty context round-trips clean
  (h) pipeline_context_round_trip_full  — full context (all node types, multiple branches) round-trips clean
  (i) validate_ok                      — valid context returns no errors
  (j) validate_empty_ceremony          — empty ceremony string caught
  (k) validate_invalid_node_type       — node with invalid type caught
  (l) nodes_from_receipt_schema        — nodes built with receipt_schema helpers are accepted
  (m) disposition_values               — single-session / chain-terminal / empty string all accepted
  (n) branch_legible_true_false        — legible flag round-trips correctly
  (o) branch_evidence_deep_copy        — mutating source dict does NOT affect stored branch evidence
  (p) nodes_deep_copy                  — mutating source node does NOT affect stored ledger entry
  (q) applicable_node_ids_round_trip   — declared-membership list survives to_dict/from_dict;
                                         from_dict tolerates absence of the key (old data → [])
  (r) consumed_handoff_predecessor_round_trip — consumed_handoff/predecessor survive
                                         to_dict/from_dict; default to "" when unset
  (s) sid                              — sid survives to_dict/from_dict; defaults to ""
                                         when unset; closes resolved_state.sid = null defect

Spec backlink:
  coordinator_core/ops/ceremony/pipeline_context.py
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § C2.1
  docs/plans/2026-07-08-wsc-commit-op-defects.md § Bug-1(i)
  docs/plans/2026-07-10-wsc-resolve-foreign-repo-bleed-and-sid-null.md
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony.pipeline_context import (
    BRANCH_ID_CHAIN_SLUG,
    BRANCH_ID_GOVERNING_PLAN,
    BRANCH_ID_WSC_DISPOSITION,
    BranchResolution,
    PipelineContext,
)
from coordinator_core.ops.ceremony.receipt_schema import (
    make_b_node,
    make_d_node,
    make_f_node,
    make_j_node,
    make_x_node,
)

# ---------------------------------------------------------------------------
# (a) BranchResolution construction — valid node_type accepted; invalid rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_type", ["D", "J", "F", "B", "X"])
def test_branch_resolution_valid_node_type(node_type: str) -> None:
    """BranchResolution accepts all five valid node_type values."""
    br = BranchResolution(
        branch_id="test-branch",
        legible=True,
        node_type=node_type,
    )
    assert br.node_type == node_type


def test_branch_resolution_invalid_node_type_raises() -> None:
    """BranchResolution.__post_init__ raises ValueError for an unknown node_type."""
    with pytest.raises(ValueError, match="node_type"):
        BranchResolution(branch_id="test", legible=True, node_type="Z")


def test_branch_resolution_defaults() -> None:
    """BranchResolution defaults: signal_read=None, evidence={}."""
    br = BranchResolution(branch_id="b", legible=False, node_type="X")
    assert br.signal_read is None
    assert br.evidence == {}


# ---------------------------------------------------------------------------
# (b) BranchResolution round-trip — to_dict / from_dict is lossless
# ---------------------------------------------------------------------------


def test_branch_resolution_round_trip_minimal() -> None:
    """A minimal BranchResolution round-trips without information loss."""
    original = BranchResolution(
        branch_id=BRANCH_ID_WSC_DISPOSITION,
        legible=True,
        node_type="D",
    )
    restored = BranchResolution.from_dict(original.to_dict())
    assert restored.branch_id == original.branch_id
    assert restored.legible == original.legible
    assert restored.node_type == original.node_type
    assert restored.signal_read == original.signal_read
    assert restored.evidence == original.evidence


def test_branch_resolution_round_trip_full() -> None:
    """A fully-populated BranchResolution round-trips without information loss."""
    original = BranchResolution(
        branch_id=BRANCH_ID_GOVERNING_PLAN,
        legible=True,
        node_type="J",
        signal_read=["docs/plans/2026-07-06-example.md"],
        evidence={
            "method": "file-existence-check",
            "question": "Was a plan doc the subject of this session?",
            "path": "docs/plans/2026-07-06-example.md",
        },
    )
    data = original.to_dict()
    restored = BranchResolution.from_dict(data)
    assert restored.branch_id == original.branch_id
    assert restored.legible is True
    assert restored.node_type == "J"
    assert restored.signal_read == ["docs/plans/2026-07-06-example.md"]
    assert restored.evidence["method"] == "file-existence-check"


def test_branch_resolution_round_trip_x_node() -> None:
    """An X-node BranchResolution (illegible state) round-trips correctly."""
    original = BranchResolution(
        branch_id=BRANCH_ID_CHAIN_SLUG,
        legible=False,
        node_type="X",
        signal_read=None,
        evidence={"missing_signal": "SESSION_START_TIME"},
    )
    restored = BranchResolution.from_dict(original.to_dict())
    assert restored.legible is False
    assert restored.node_type == "X"
    assert restored.signal_read is None
    assert restored.evidence["missing_signal"] == "SESSION_START_TIME"


# ---------------------------------------------------------------------------
# (c) PipelineContext construction — defaults
# ---------------------------------------------------------------------------


def test_pipeline_context_construction_defaults() -> None:
    """PipelineContext initialises with empty lists and empty disposition."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.ceremony == "wsc"
    assert ctx.scope_mode == "architecture"
    assert ctx.disposition == ""
    assert ctx.resolved_branches == []
    assert ctx.nodes == []


def test_pipeline_context_construction_explicit() -> None:
    """PipelineContext accepts all fields explicitly."""
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="standard",
        disposition="chain-terminal",
        resolved_branches=[],
        nodes=[],
    )
    assert ctx.disposition == "chain-terminal"


# ---------------------------------------------------------------------------
# (d) add_branch / get_branch accessors
# ---------------------------------------------------------------------------


def test_add_branch_and_get_branch() -> None:
    """add_branch appends; get_branch retrieves by branch_id."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    br = BranchResolution(
        branch_id=BRANCH_ID_WSC_DISPOSITION,
        legible=True,
        node_type="D",
        signal_read="single-session",
    )
    ctx.add_branch(br)
    found = ctx.get_branch(BRANCH_ID_WSC_DISPOSITION)
    assert found is br


def test_get_branch_absent_returns_none() -> None:
    """get_branch returns None for an unknown branch_id."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.get_branch("nonexistent") is None


def test_add_multiple_branches_preserved() -> None:
    """Multiple branches can be added and retrieved independently."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    br1 = BranchResolution(branch_id="A", legible=True, node_type="D")
    br2 = BranchResolution(branch_id="B", legible=False, node_type="X")
    ctx.add_branch(br1)
    ctx.add_branch(br2)
    assert len(ctx.resolved_branches) == 2
    assert ctx.get_branch("A") is br1
    assert ctx.get_branch("B") is br2


# ---------------------------------------------------------------------------
# (e) add_node / get_node accessors; receipt_schema shapes accepted
# ---------------------------------------------------------------------------


def test_add_node_and_get_node() -> None:
    """add_node appends; get_node retrieves by node id."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    node = make_d_node("step-0", resolving_op="wsc_session_init",
                       evidence={"disposition": "single-session"})
    ctx.add_node(node)
    found = ctx.get_node("step-0")
    assert found is not None
    assert found["type"] == "D"
    assert found["resolving_op"] == "wsc_session_init"


def test_get_node_absent_returns_none() -> None:
    """get_node returns None when the node_id is not in the ledger."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.get_node("nonexistent") is None


def test_add_all_node_types() -> None:
    """All five node types can be added to the ledger."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.add_node(make_d_node("d1"))
    ctx.add_node(make_j_node("j1", question="Is this generalizable?"))
    ctx.add_node(make_f_node("f1", slot="lesson_body"))
    ctx.add_node(make_b_node("b1"))
    ctx.add_node(make_x_node("x1", missing_signal="SESSION_START_TIME"))
    assert len(ctx.nodes) == 5
    types = {n["type"] for n in ctx.nodes}
    assert types == {"D", "J", "F", "B", "X"}


# ---------------------------------------------------------------------------
# (f) PipelineContext round-trip — to_dict / from_dict
# ---------------------------------------------------------------------------


def test_pipeline_context_round_trip_disposition() -> None:
    """disposition round-trips correctly."""
    for disp in ("single-session", "chain-terminal", ""):
        ctx = PipelineContext(ceremony="wsc", scope_mode="architecture",
                              disposition=disp)
        restored = PipelineContext.from_dict(ctx.to_dict())
        assert restored.disposition == disp, (
            f"disposition {disp!r} did not survive round-trip"
        )


def test_pipeline_context_round_trip_preserves_nodes() -> None:
    """Node ledger survives round-trip without data loss."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture",
                          disposition="single-session")
    ctx.add_node(make_d_node("step-0", resolving_op="wsc_session_init",
                              evidence={"disposition": "single-session"}))
    ctx.add_node(make_j_node("step-1a",
                              question="Is this generalizable?",
                              answer=""))
    ctx.add_node(make_d_node("step-3.5a",
                              resolving_op="cs_archive",
                              evidence={"acted": ["sid-abc"], "skipped": [], "failed": []},
                              tail_step=True))

    data = ctx.to_dict()
    restored = PipelineContext.from_dict(data)

    assert len(restored.nodes) == 3
    step0 = restored.get_node("step-0")
    assert step0 is not None
    assert step0["evidence"]["disposition"] == "single-session"
    step35a = restored.get_node("step-3.5a")
    assert step35a is not None
    assert step35a["tail_step"] is True


def test_pipeline_context_round_trip_preserves_branches() -> None:
    """resolved_branches survive round-trip without data loss."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_WSC_DISPOSITION,
        legible=True,
        node_type="D",
        signal_read="chain-terminal",
        evidence={"method": "grep", "path": "state/handoffs/"},
    ))
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_CHAIN_SLUG,
        legible=False,
        node_type="X",
        signal_read=None,
        evidence={"missing_signal": "SESSION_START_TIME"},
    ))

    data = ctx.to_dict()
    restored = PipelineContext.from_dict(data)

    assert len(restored.resolved_branches) == 2
    disp_br = restored.get_branch(BRANCH_ID_WSC_DISPOSITION)
    assert disp_br is not None
    assert disp_br.signal_read == "chain-terminal"
    chain_br = restored.get_branch(BRANCH_ID_CHAIN_SLUG)
    assert chain_br is not None
    assert chain_br.legible is False


# ---------------------------------------------------------------------------
# (g) empty context round-trips clean
# ---------------------------------------------------------------------------


def test_pipeline_context_round_trip_empty() -> None:
    """An empty PipelineContext (no nodes, no branches) round-trips cleanly."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    restored = PipelineContext.from_dict(ctx.to_dict())
    assert restored.ceremony == "wsc"
    assert restored.scope_mode == "architecture"
    assert restored.disposition == ""
    assert restored.resolved_branches == []
    assert restored.nodes == []


# ---------------------------------------------------------------------------
# (h) full context (all node types, multiple branches) round-trips clean
# ---------------------------------------------------------------------------


def test_pipeline_context_round_trip_full() -> None:
    """A context containing all five node types and multiple branches round-trips clean."""
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="architecture",
        disposition="chain-terminal",
    )
    # Nodes
    ctx.add_node(make_d_node("step-0", resolving_op="wsc_session_init",
                              evidence={"disposition": "chain-terminal"},
                              tail_step=False))
    ctx.add_node(make_j_node("step-1a", question="Is this generalizable?"))
    ctx.add_node(make_f_node("step-1b", slot="lesson_body"))
    ctx.add_node(make_b_node("B1",
                              pre_resolved_evidence={"slice_count": 1},
                              em_adjudication={}))
    ctx.add_node(make_x_node("step-2.6.3", missing_signal="SESSION_START_TIME"))
    ctx.add_node(make_d_node("step-3.5a", resolving_op="cs_archive",
                              evidence={"acted": ["s-123"], "skipped": [], "failed": []},
                              tail_step=True))
    # Branches
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_WSC_DISPOSITION,
        legible=True,
        node_type="D",
        signal_read="chain-terminal",
    ))
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_GOVERNING_PLAN,
        legible=True,
        node_type="J",
        signal_read=True,
        evidence={"question": "Was a plan doc the subject of this session?"},
    ))

    data = ctx.to_dict()
    restored = PipelineContext.from_dict(data)

    assert restored.ceremony == "wsc"
    assert restored.disposition == "chain-terminal"
    assert len(restored.nodes) == 6
    assert len(restored.resolved_branches) == 2
    assert restored.get_node("B1") is not None
    assert restored.get_node("step-3.5a")["tail_step"] is True
    assert restored.get_branch(BRANCH_ID_GOVERNING_PLAN).signal_read is True


# ---------------------------------------------------------------------------
# (i) validate — valid context returns no errors
# ---------------------------------------------------------------------------


def test_validate_valid_context() -> None:
    """A correctly constructed PipelineContext validates with no errors."""
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="architecture",
        disposition="single-session",
        sid="sess-001",
    )
    ctx.add_node(make_d_node("step-0"))
    ctx.add_branch(BranchResolution(
        branch_id=BRANCH_ID_WSC_DISPOSITION,
        legible=True,
        node_type="D",
        signal_read="single-session",
    ))
    errors = ctx.validate()
    assert errors == [], f"Expected no validation errors; got: {errors}"


# ---------------------------------------------------------------------------
# (j) validate — empty ceremony string caught
# ---------------------------------------------------------------------------


def test_validate_empty_ceremony() -> None:
    """validate returns an error when ceremony is an empty string."""
    ctx = PipelineContext(ceremony="", scope_mode="architecture")
    errors = ctx.validate()
    assert any("ceremony" in e for e in errors), (
        f"Expected error mentioning 'ceremony'; got {errors}"
    )


# ---------------------------------------------------------------------------
# (k) validate — node with invalid type caught
# ---------------------------------------------------------------------------


def test_validate_invalid_node_type() -> None:
    """validate returns an error when a node has an unrecognised type."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.nodes.append({"id": "bad", "type": "Z"})
    errors = ctx.validate()
    assert any("Z" in e or "type" in e for e in errors), (
        f"Expected error mentioning invalid type; got {errors}"
    )


# ---------------------------------------------------------------------------
# (l) nodes_from_receipt_schema — receipt_schema helpers produce accepted nodes
# ---------------------------------------------------------------------------


def test_nodes_built_with_receipt_schema_helpers_validate() -> None:
    """Nodes created via receipt_schema make_* helpers validate inside a context."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture",
                          disposition="single-session", sid="sess-002")
    ctx.add_node(make_d_node("d1", resolving_op="op_a", evidence={"k": "v"}))
    ctx.add_node(make_j_node("j1", question="Q?", answer=""))
    ctx.add_node(make_f_node("f1", slot="lesson_body", filled=""))
    ctx.add_node(make_b_node("b1",
                              pre_resolved_evidence={"slices": 1},
                              em_adjudication={}))
    ctx.add_node(make_x_node("x1", missing_signal="SIG"))
    errors = ctx.validate()
    assert errors == [], f"Expected no validation errors; got: {errors}"


# ---------------------------------------------------------------------------
# (m) disposition_values — expected values accepted; empty string is valid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("disp", ["single-session", "chain-terminal", ""])
def test_disposition_values_accepted(disp: str) -> None:
    """PipelineContext accepts 'single-session', 'chain-terminal', and empty string."""
    # Review: code-reviewer F7 — sid is required once disposition is non-empty
    # (see validate()'s sid non-emptiness check); the "" disposition case
    # intentionally omits sid to also cover the pre-resolution fixture shape.
    sid = "sess-003" if disp else ""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture", disposition=disp, sid=sid)
    assert ctx.disposition == disp
    errors = ctx.validate()
    assert errors == [], f"disposition={disp!r} unexpectedly failed validation: {errors}"


# ---------------------------------------------------------------------------
# (n) legible flag round-trips correctly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("legible", [True, False])
def test_branch_legible_round_trips(legible: bool) -> None:
    """BranchResolution.legible survives to_dict/from_dict unchanged."""
    br = BranchResolution(
        branch_id="test", legible=legible, node_type="D" if legible else "X"
    )
    restored = BranchResolution.from_dict(br.to_dict())
    assert restored.legible is legible


# ---------------------------------------------------------------------------
# (o) branch evidence deep copy — mutating source does NOT affect stored evidence
# ---------------------------------------------------------------------------


def test_branch_evidence_deep_copy_on_to_dict() -> None:
    """to_dict deep-copies evidence; mutating the original dict does NOT affect the copy."""
    evidence = {"method": "grep", "value": "single-session"}
    br = BranchResolution(branch_id="b", legible=True, node_type="D", evidence=evidence)
    data = br.to_dict()
    evidence["value"] = "MUTATED"  # mutate original
    assert data["evidence"]["value"] == "single-session", (
        "to_dict() must deep-copy evidence so mutation of the source does not propagate"
    )


def test_branch_evidence_deep_copy_on_from_dict() -> None:
    """from_dict deep-copies evidence; mutating the input dict does NOT affect the result."""
    data = {
        "branch_id": "b",
        "legible": True,
        "node_type": "D",
        "signal_read": None,
        "evidence": {"method": "glob"},
    }
    br = BranchResolution.from_dict(data)
    data["evidence"]["method"] = "MUTATED"
    assert br.evidence["method"] == "glob", (
        "from_dict() must deep-copy evidence so mutation of the input does not propagate"
    )


# ---------------------------------------------------------------------------
# (p) nodes deep copy — mutating source does NOT affect stored ledger entry
# ---------------------------------------------------------------------------


def test_context_nodes_deep_copy_on_to_dict() -> None:
    """to_dict deep-copies nodes; mutating the context after to_dict does not affect the copy."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    node = make_d_node("d1", evidence={"result": "ok"})
    ctx.add_node(node)
    data = ctx.to_dict()
    ctx.nodes[0]["evidence"]["result"] = "MUTATED"
    assert data["nodes"][0]["evidence"]["result"] == "ok", (
        "to_dict() must deep-copy nodes so in-place mutation does not propagate"
    )


def test_context_nodes_deep_copy_on_from_dict() -> None:
    """from_dict deep-copies nodes; mutating the input data does not affect the context."""
    ctx_original = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx_original.add_node(make_d_node("d1", evidence={"result": "ok"}))
    data = ctx_original.to_dict()
    ctx_restored = PipelineContext.from_dict(data)
    data["nodes"][0]["evidence"]["result"] = "MUTATED"
    assert ctx_restored.nodes[0]["evidence"]["result"] == "ok", (
        "from_dict() must deep-copy nodes so mutation of the input dict does not propagate"
    )


# ---------------------------------------------------------------------------
# (q) applicable_node_ids — declared-membership field round-trip (op-spec §3)
# ---------------------------------------------------------------------------


def test_applicable_node_ids_defaults_empty() -> None:
    """A freshly constructed PipelineContext has an empty applicable_node_ids list."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.applicable_node_ids == []


def test_applicable_node_ids_round_trip() -> None:
    """applicable_node_ids survives to_dict() -> from_dict() without data loss."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture",
                          disposition="single-session")
    ctx.add_node(make_d_node("step-0", resolving_op="wsc_session_init"))
    ctx.applicable_node_ids = ["step-0"]

    restored = PipelineContext.from_dict(ctx.to_dict())
    assert restored.applicable_node_ids == ["step-0"]


def test_applicable_node_ids_from_dict_tolerates_absent_key() -> None:
    """from_dict() defaults applicable_node_ids to [] when the key is absent
    (pre-existing serialized state written before this field existed).
    """
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "single-session",
        "resolved_branches": [],
        "nodes": [],
        # "applicable_node_ids" deliberately omitted
    }
    restored = PipelineContext.from_dict(data)
    assert restored.applicable_node_ids == []


# ---------------------------------------------------------------------------
# (r) consumed_handoff / predecessor — chain-terminal linkage fields
# ---------------------------------------------------------------------------


def test_consumed_handoff_predecessor_default_empty() -> None:
    """A freshly constructed PipelineContext has empty consumed_handoff/predecessor."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.consumed_handoff == ""
    assert ctx.predecessor == ""


def test_consumed_handoff_predecessor_round_trip() -> None:
    """consumed_handoff/predecessor survive to_dict() -> from_dict() without loss."""
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="architecture",
        disposition="chain-terminal",
    )
    ctx.consumed_handoff = "state/handoffs/consumed.md"
    ctx.predecessor = "sess-predecessor-001"

    data = ctx.to_dict()
    assert data["consumed_handoff"] == "state/handoffs/consumed.md"
    assert data["predecessor"] == "sess-predecessor-001"

    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == "state/handoffs/consumed.md"
    assert restored.predecessor == "sess-predecessor-001"


def test_consumed_handoff_predecessor_from_dict_tolerates_both_keys_absent() -> None:
    """from_dict() defaults consumed_handoff/predecessor to "" when both keys are
    absent (pre-existing serialized state written before these fields existed).
    """
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "single-session",
        "resolved_branches": [],
        "nodes": [],
        # "consumed_handoff" / "predecessor" deliberately omitted
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == ""
    assert restored.predecessor == ""


def test_consumed_handoff_predecessor_from_dict_tolerates_consumed_handoff_absent() -> None:
    """from_dict() defaults consumed_handoff to "" when only that key is absent
    (predecessor present) — the two fields are read via independent data.get() calls.
    """
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "chain-terminal",
        "resolved_branches": [],
        "nodes": [],
        "predecessor": "sess-predecessor-001",
        # "consumed_handoff" deliberately omitted
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == ""
    assert restored.predecessor == "sess-predecessor-001"


def test_consumed_handoff_predecessor_from_dict_tolerates_predecessor_absent() -> None:
    """from_dict() defaults predecessor to "" when only that key is absent
    (consumed_handoff present) — the two fields are read via independent data.get() calls.
    """
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "chain-terminal",
        "resolved_branches": [],
        "nodes": [],
        "consumed_handoff": "state/handoffs/consumed.md",
        # "predecessor" deliberately omitted
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == "state/handoffs/consumed.md"
    assert restored.predecessor == ""


def test_from_dict_explicit_empty_plural_list_drops_stale_scalar() -> None:
    """Review: code-reviewer (Slice C1, Finding 1/2) — a hand-edited or
    partially-migrated dict carrying an EXPLICIT empty `consumed_handoffs: []`
    alongside a non-empty legacy `consumed_handoff` scalar must NOT resurrect
    the stale scalar into the plural list. from_dict() reads the plural key
    by presence, not truthiness: an explicit `[]` means "zero handoffs".
    """
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "chain-terminal",
        "resolved_branches": [],
        "nodes": [],
        "consumed_handoffs": [],
        "consumed_handoff": "state/handoffs/x.md",
        "predecessors": [],
        "predecessor": "sess-stale-001",
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoffs == []
    assert restored.consumed_handoff == ""
    assert restored.predecessors == []
    assert restored.predecessor == ""


def test_validate_rejects_scalar_plural_divergence_via_f4() -> None:
    """Review: code-reviewer (Slice C1, Finding 1/2) — a hand-constructed
    PipelineContext where the scalar and list[0] diverge must fail validate()
    via the Patrik F4 consistency check (not silently pass).
    """
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="architecture",
        disposition="chain-terminal",
        consumed_handoff="X",
        consumed_handoffs=["Y"],
    )
    errors = ctx.validate()
    assert any("consumed_handoff must equal consumed_handoffs[0]" in e for e in errors)


def test_validate_memo_predecessor_disposition_with_empty_consumed_handoffs_passes() -> None:
    """A `disposition="memo-predecessor"` context with empty `consumed_handoffs`/
    `predecessors` must NOT trip the chain-terminal-emptiness invariant.

    Spec: docs/plans/2026-08-05-memo-predecessor-representable-outcome.md §
    Fix-locus discrimination — on the memo leg `consumed_handoff` is
    contractually "" and `consumed_handoff_paths` is [] (the memo path is
    carried ONLY on the upstream `.detection` record, never here).
    `"memo-predecessor"` is now a member of `wsc_disposition.VALID`, so
    `disposition_for_check` canonicalizes it (to itself, per C1) rather than
    falling back to the raw string; either way it is not equal to
    `PREDECESSOR_CONSUMED`, so the invariant's guard clause
    `(self.consumed_handoffs or self.predecessors)` — both empty here — is
    what keeps this context valid, exactly the same as any other
    non-chain-terminal disposition. No code change was needed at this site;
    this test locks the already-correct behaviour in.
    """
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="architecture",
        disposition="memo-predecessor",
        sid="sess-memo-001",
    )
    errors = ctx.validate()
    assert errors == [], f"Expected no validation errors; got: {errors}"


# ---------------------------------------------------------------------------
# (s) sid — resolved_state.sid = null defect fix (docs/plans/2026-07-10-
#     wsc-resolve-foreign-repo-bleed-and-sid-null.md § Defect B)
# ---------------------------------------------------------------------------


def test_sid_defaults_empty() -> None:
    """A freshly constructed PipelineContext has an empty sid."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.sid == ""


def test_sid_carried_in_to_dict() -> None:
    """to_dict() carries sid — closes the resolved_state.sid = null defect."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.sid = "sess-example-001"
    data = ctx.to_dict()
    assert data["sid"] == "sess-example-001"


def test_sid_round_trip() -> None:
    """sid survives to_dict() -> from_dict() without loss."""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.sid = "sess-round-trip-001"

    data = ctx.to_dict()
    assert data["sid"] == "sess-round-trip-001"

    restored = PipelineContext.from_dict(data)
    assert restored.sid == "sess-round-trip-001"


def test_sid_from_dict_tolerates_absent_key() -> None:
    """from_dict() defaults sid to "" when the key is absent (pre-existing
    serialized state written before this field existed).
    """
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "single-session",
        "resolved_branches": [],
        "nodes": [],
        # "sid" deliberately omitted
    }
    restored = PipelineContext.from_dict(data)
    assert restored.sid == ""
