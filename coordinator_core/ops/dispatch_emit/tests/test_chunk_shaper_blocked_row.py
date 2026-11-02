"""A row carrying blocked_on / depends_on_plan is never merged by the emit chunk shaper."""

from coordinator_core.ops.dispatch_emit.emit import shape_rows_for_emit
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow


def _row(rid: str, writes: list) -> EmitterRow:
    return EmitterRow(
        id=rid, title=rid, surface="x", writes=writes, reads=[], depends_on=[],
        agent_type="coordinator-executor", agent_model="sonnet", body="small",
    )


def _merged_ids(raws: dict) -> list:
    return [r for r, raw in raws.items() if isinstance(raw, dict) and raw.get("constituents")]


def test_unblocked_pair_sharing_a_write_merges():
    rows = [_row("A", ["a.py"]), _row("B", ["a.py"])]
    shaped, _raws, merges = shape_rows_for_emit(rows, {"A": {}, "B": {}})
    assert len(merges) == 1 and len(shaped) == 1


def test_blocked_on_row_is_never_merged():
    rows = [_row("A", ["a.py"]), _row("B", ["a.py"])]
    raws = {"A": {"blocked_on": {"capability": "roster not committed"}}, "B": {}}
    shaped, out, merges = shape_rows_for_emit(rows, raws)
    assert merges == [] and [r.id for r in shaped] == ["A", "B"]
    assert [r.agent_model for r in shaped] == ["sonnet", "sonnet"]
    assert _merged_ids(out) == []


def test_depends_on_plan_row_is_never_merged_even_when_disjoint():
    rows = [_row("A", ["a.py"]), _row("B", ["b.py"])]
    raws = {"A": {}, "B": {"depends_on_plan": [{"plan": "docs/p.md", "chunk": "C1"}]}}
    shaped, _out, merges = shape_rows_for_emit(rows, raws)
    assert merges == [] and len(shaped) == 2


def test_disjoint_unblocked_rows_still_merge():
    rows = [_row("A", ["a.py"]), _row("B", ["b.py"])]
    _shaped, _out, merges = shape_rows_for_emit(rows, {"A": {}, "B": {}})
    assert len(merges) == 1
