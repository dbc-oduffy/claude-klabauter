"""The chunk shaper never fuses rows whose derived agent-type lanes differ."""

import pytest

from coordinator_core.ops.dispatch_emit.chunk_shaper import (
    _candidate,
    _disjoint_candidate,
    shape_rows,
)
from coordinator_core.ops.dispatch_emit.emit import (
    MixedAgentTypeRowError,
    _row_agent_type,
    derived_agent_lane,
    emit_script,
    shape_rows_for_emit,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import _V5_ROSTER_FRAGMENT, _V5_STAGE_SCHEMAS
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import build_waves

_BODY_PATH = "docs/plans/2026-11/x.corpus-basis.md"


def _row(rid: str, writes: list, **kw) -> EmitterRow:
    kw.setdefault("agent_type", None)
    return EmitterRow(
        id=rid, title=rid, surface="x", writes=writes, reads=[], depends_on=[],
        agent_model=None, body="small", **kw,
    )


def _wave_of(rows: list) -> dict:
    return {r.id: n for n, w in enumerate(build_waves(rows)) for r in w}


def test_disjoint_lane_mixed_pair_is_not_a_candidate():
    a, b = _row("A", [_BODY_PATH]), _row("B", ["src/b.py"])
    assert not _disjoint_candidate(a, b, _wave_of([a, b]))


def test_shared_write_lane_mixed_pair_is_not_a_candidate():
    a = _row("A", [_BODY_PATH, "src/a.py"])
    b = _row("B", ["src/a.py"])
    assert not _candidate(a, b)


def test_same_lane_disjoint_pair_still_merges():
    a, b = _row("A", ["src/a.py"]), _row("B", ["src/b.py"])
    assert _disjoint_candidate(a, b, _wave_of([a, b]))
    assert len(shape_rows([a, b]).merges) == 1


def test_same_lane_enricher_pair_still_merges():
    a, b = _row("A", [_BODY_PATH]), _row("B", ["docs/plans/2026-11/y.corpus-basis.md"])
    assert len(shape_rows([a, b]).merges) == 1


def test_three_rows_fuse_only_within_lane_and_every_result_derives():
    rows = [
        _row("C3", ["src/loop_one.py", "src/loop_one.schema.json"]),
        _row("C2", ["docs/delivery/mhmi-05.md"]),
        _row("D2", [_BODY_PATH]),
    ]
    shaped = shape_rows(rows)
    assert [set(m_ids) for _, m_ids in shaped.merges] == [{"C3", "C2"}]
    for row in shaped.rows:
        _row_agent_type(row)


def test_corpus_basis_sidecar_is_enricher_lane_by_the_guard_regex():
    """R1 defect 2, decided: a docs/plans corpus-basis sidecar stays an immutable body (`_PLAN_BODY_RE` unchanged), so enricher work; the lane split is what lets it emit."""
    assert derived_agent_lane(_row("D2", [_BODY_PATH])) == "coordinator:enricher"
    assert derived_agent_lane(_row("C2", ["docs/delivery/x.md"])) == "coordinator:executor"
    assert derived_agent_lane(_row("E", [_BODY_PATH], agent_type="coordinator:workflow-maker")) == (
        "coordinator:workflow-maker"
    )


def test_a_hand_fused_lane_mixed_row_still_refuses():
    with pytest.raises(MixedAgentTypeRowError):
        _row_agent_type(_row("C1-C2", [_BODY_PATH, "src/a.py"]))


def test_shape_rows_for_emit_keeps_a_doc_lane_row_and_a_code_row_apart():
    rows = [_row("C1", ["src/a.py"]), _row("C2", [_BODY_PATH])]
    shaped, _raws, merges = shape_rows_for_emit(rows, {"C1": {}, "C2": {}})
    assert merges == [] and [r.id for r in shaped] == ["C1", "C2"]


def test_emit_plan_with_one_doc_lane_row_and_one_code_row_emits_without_hold(tmp_path):
    """N2: a single plan carrying both lanes emits; the rows stay separate chunks."""
    plan = tmp_path / "plan.md"
    plan.write_text(
        "# fixture plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: code row\n  change_kind: code-edit\n  surface: src\n"
        "  writes:\n    - src/a.py\n"
        "- id: C2\n  title: corpus basis\n  change_kind: doc-edit\n  surface: docs\n"
        f"  writes:\n    - {_BODY_PATH}\n"
        "```\n",
        encoding="utf-8",
    )
    findings: list = []
    script = emit_script(
        plan, repo_root=tmp_path, findings_out=findings, review_roster_fragment=_V5_ROSTER_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )
    assert "agentType: 'coordinator:enricher'" in script
    assert "agentType: 'coordinator:executor'" in script
    assert not [f for f in findings if f.code == "CHUNK-SHAPER-MERGED"]
    assert "'C1'" in script and "'C2'" in script


def test_emit_plan_with_many_mixed_lane_rows_stays_under_the_script_cap(tmp_path):
    """Many alternating doc-lane and code rows emit without a mixed-lane or byte-cap refusal."""
    from coordinator_core.ops.dispatch_emit.emit import _WORKFLOW_SCRIPT_BYTE_CAP

    tasks = ""
    for i in range(8):
        tasks += (
            f"- id: K{i}\n  title: code {i}\n  change_kind: code-edit\n  surface: src\n"
            f"  writes:\n    - src/m{i}.py\n"
            f"- id: D{i}\n  title: basis {i}\n  change_kind: doc-edit\n  surface: docs\n"
            f"  writes:\n    - docs/plans/2026-11/p{i}.corpus-basis.md\n"
        )
    plan = tmp_path / "plan.md"
    plan.write_text(f"# fixture plan\n\n## Tasks\n\n```yaml plan-tasks\n{tasks}```\n", encoding="utf-8")
    script = emit_script(
        plan, repo_root=tmp_path, findings_out=[], review_roster_fragment=_V5_ROSTER_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )
    assert len(script.encode("utf-8")) <= _WORKFLOW_SCRIPT_BYTE_CAP
    assert "agentType: 'coordinator:enricher'" in script and "agentType: 'coordinator:executor'" in script
