
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


@pytest.mark.parametrize("node_type", ["D", "J", "F", "B", "X"])
def test_branch_resolution_valid_node_type(node_type: str) -> None:
    br = BranchResolution(
        branch_id="test-branch",
        legible=True,
        node_type=node_type,
    )
    assert br.node_type == node_type


def test_branch_resolution_invalid_node_type_raises() -> None:
    with pytest.raises(ValueError, match="node_type"):
        BranchResolution(branch_id="test", legible=True, node_type="Z")


def test_branch_resolution_defaults() -> None:
    br = BranchResolution(branch_id="b", legible=False, node_type="X")
    assert br.signal_read is None
    assert br.evidence == {}


def test_branch_resolution_round_trip_minimal() -> None:
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


def test_pipeline_context_construction_defaults() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.ceremony == "wsc"
    assert ctx.scope_mode == "architecture"
    assert ctx.disposition == ""
    assert ctx.resolved_branches == []
    assert ctx.nodes == []


def test_pipeline_context_construction_explicit() -> None:
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="standard",
        disposition="chain-terminal",
        resolved_branches=[],
        nodes=[],
    )
    assert ctx.disposition == "chain-terminal"


def test_add_branch_and_get_branch() -> None:
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
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.get_branch("nonexistent") is None


def test_add_multiple_branches_preserved() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    br1 = BranchResolution(branch_id="A", legible=True, node_type="D")
    br2 = BranchResolution(branch_id="B", legible=False, node_type="X")
    ctx.add_branch(br1)
    ctx.add_branch(br2)
    assert len(ctx.resolved_branches) == 2
    assert ctx.get_branch("A") is br1
    assert ctx.get_branch("B") is br2


def test_add_node_and_get_node() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    node = make_d_node("step-0", resolving_op="wsc_session_init",
                       evidence={"disposition": "single-session"})
    ctx.add_node(node)
    found = ctx.get_node("step-0")
    assert found is not None
    assert found["type"] == "D"
    assert found["resolving_op"] == "wsc_session_init"


def test_get_node_absent_returns_none() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.get_node("nonexistent") is None


def test_add_all_node_types() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.add_node(make_d_node("d1"))
    ctx.add_node(make_j_node("j1", question="Is this generalizable?"))
    ctx.add_node(make_f_node("f1", slot="lesson_body"))
    ctx.add_node(make_b_node("b1"))
    ctx.add_node(make_x_node("x1", missing_signal="SESSION_START_TIME"))
    assert len(ctx.nodes) == 5
    types = {n["type"] for n in ctx.nodes}
    assert types == {"D", "J", "F", "B", "X"}


def test_pipeline_context_round_trip_disposition() -> None:
    for disp in ("single-session", "chain-terminal", ""):
        ctx = PipelineContext(ceremony="wsc", scope_mode="architecture",
                              disposition=disp)
        restored = PipelineContext.from_dict(ctx.to_dict())
        assert restored.disposition == disp, (
            f"disposition {disp!r} did not survive round-trip"
        )


def test_pipeline_context_round_trip_preserves_nodes() -> None:
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


def test_pipeline_context_round_trip_empty() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    restored = PipelineContext.from_dict(ctx.to_dict())
    assert restored.ceremony == "wsc"
    assert restored.scope_mode == "architecture"
    assert restored.disposition == ""
    assert restored.resolved_branches == []
    assert restored.nodes == []


def test_pipeline_context_round_trip_full() -> None:
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="architecture",
        disposition="chain-terminal",
    )
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


def test_validate_valid_context() -> None:
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


def test_validate_empty_ceremony() -> None:
    ctx = PipelineContext(ceremony="", scope_mode="architecture")
    errors = ctx.validate()
    assert any("ceremony" in e for e in errors), (
        f"Expected error mentioning 'ceremony'; got {errors}"
    )


def test_validate_invalid_node_type() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.nodes.append({"id": "bad", "type": "Z"})
    errors = ctx.validate()
    assert any("Z" in e or "type" in e for e in errors), (
        f"Expected error mentioning invalid type; got {errors}"
    )


def test_nodes_built_with_receipt_schema_helpers_validate() -> None:
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


@pytest.mark.parametrize("disp", ["single-session", "chain-terminal", ""])
def test_disposition_values_accepted(disp: str) -> None:
    sid = "sess-003" if disp else ""
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture", disposition=disp, sid=sid)
    assert ctx.disposition == disp
    errors = ctx.validate()
    assert errors == [], f"disposition={disp!r} unexpectedly failed validation: {errors}"


@pytest.mark.parametrize("legible", [True, False])
def test_branch_legible_round_trips(legible: bool) -> None:
    br = BranchResolution(
        branch_id="test", legible=legible, node_type="D" if legible else "X"
    )
    restored = BranchResolution.from_dict(br.to_dict())
    assert restored.legible is legible


def test_branch_evidence_deep_copy_on_to_dict() -> None:
    evidence = {"method": "grep", "value": "single-session"}
    br = BranchResolution(branch_id="b", legible=True, node_type="D", evidence=evidence)
    data = br.to_dict()
    evidence["value"] = "MUTATED"
    assert data["evidence"]["value"] == "single-session", (
        "to_dict() must deep-copy evidence so mutation of the source does not propagate"
    )


def test_branch_evidence_deep_copy_on_from_dict() -> None:
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


def test_context_nodes_deep_copy_on_to_dict() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    node = make_d_node("d1", evidence={"result": "ok"})
    ctx.add_node(node)
    data = ctx.to_dict()
    ctx.nodes[0]["evidence"]["result"] = "MUTATED"
    assert data["nodes"][0]["evidence"]["result"] == "ok", (
        "to_dict() must deep-copy nodes so in-place mutation does not propagate"
    )


def test_context_nodes_deep_copy_on_from_dict() -> None:
    ctx_original = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx_original.add_node(make_d_node("d1", evidence={"result": "ok"}))
    data = ctx_original.to_dict()
    ctx_restored = PipelineContext.from_dict(data)
    data["nodes"][0]["evidence"]["result"] = "MUTATED"
    assert ctx_restored.nodes[0]["evidence"]["result"] == "ok", (
        "from_dict() must deep-copy nodes so mutation of the input dict does not propagate"
    )


def test_applicable_node_ids_defaults_empty() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.applicable_node_ids == []


def test_applicable_node_ids_round_trip() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture",
                          disposition="single-session")
    ctx.add_node(make_d_node("step-0", resolving_op="wsc_session_init"))
    ctx.applicable_node_ids = ["step-0"]

    restored = PipelineContext.from_dict(ctx.to_dict())
    assert restored.applicable_node_ids == ["step-0"]


def test_applicable_node_ids_from_dict_tolerates_absent_key() -> None:
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "single-session",
        "resolved_branches": [],
        "nodes": [],
    }
    restored = PipelineContext.from_dict(data)
    assert restored.applicable_node_ids == []


def test_consumed_handoff_predecessor_default_empty() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.consumed_handoff == ""
    assert ctx.predecessor == ""


def test_consumed_handoff_predecessor_round_trip() -> None:
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
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "single-session",
        "resolved_branches": [],
        "nodes": [],
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == ""
    assert restored.predecessor == ""


def test_consumed_handoff_predecessor_from_dict_tolerates_consumed_handoff_absent() -> None:
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "chain-terminal",
        "resolved_branches": [],
        "nodes": [],
        "predecessor": "sess-predecessor-001",
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == ""
    assert restored.predecessor == "sess-predecessor-001"


def test_consumed_handoff_predecessor_from_dict_tolerates_predecessor_absent() -> None:
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "chain-terminal",
        "resolved_branches": [],
        "nodes": [],
        "consumed_handoff": "state/handoffs/consumed.md",
    }
    restored = PipelineContext.from_dict(data)
    assert restored.consumed_handoff == "state/handoffs/consumed.md"
    assert restored.predecessor == ""


def test_from_dict_explicit_empty_plural_list_drops_stale_scalar() -> None:
    """A hand-edited or
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


def test_sid_defaults_empty() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    assert ctx.sid == ""


def test_sid_carried_in_to_dict() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.sid = "sess-example-001"
    data = ctx.to_dict()
    assert data["sid"] == "sess-example-001"


def test_sid_round_trip() -> None:
    ctx = PipelineContext(ceremony="wsc", scope_mode="architecture")
    ctx.sid = "sess-round-trip-001"

    data = ctx.to_dict()
    assert data["sid"] == "sess-round-trip-001"

    restored = PipelineContext.from_dict(data)
    assert restored.sid == "sess-round-trip-001"


def test_sid_from_dict_tolerates_absent_key() -> None:
    data = {
        "ceremony": "wsc",
        "scope_mode": "architecture",
        "disposition": "single-session",
        "resolved_branches": [],
        "nodes": [],
    }
    restored = PipelineContext.from_dict(data)
    assert restored.sid == ""
