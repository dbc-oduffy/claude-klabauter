"""check_specs: one unpinned check spec per row, rule carried by marker only."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import predispatch
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row(rid: str, body: str = "Spec: docs/plans/p.md (x)\nDo the thing.") -> WaveRow:
    return WaveRow(
        id=rid,
        title=f"title {rid}",
        surface="a.py",
        writes=["b.py", "a.py"],
        reads=[],
        depends_on=[],
        body=body,
    )


def test_one_spec_per_row_with_contract_fields():
    rows = [_row("p.C1"), _row("p.C2")]
    specs = predispatch.check_specs(rows)
    assert len(specs) == len(rows)
    for row, spec in zip(rows, specs):
        assert spec.key == row.id
        assert spec.label == f"check:{row.id}"
        assert spec.phase == predispatch.CHECK_PHASE_TITLE
        assert spec.agent_type is None
        assert spec.model == predispatch.CHECK_AGENT_MODEL
        assert spec.schema == "predispatch_check_result"


def test_labels_unique():
    specs = predispatch.check_specs([_row(f"p.C{i}") for i in range(5)])
    assert len({s.label for s in specs}) == 5


def test_prompt_carries_row_text_and_marker_not_rule():
    (spec,) = predispatch.check_specs([_row("p.C1")])
    assert "Do the thing." in spec.prompt
    assert "a.py, b.py" in spec.prompt
    assert "docs/plans/p.md" in spec.prompt
    assert spec.prompt.count(predispatch.ALREADY_DONE_RULE_MARKER) == 1
    assert predispatch.ALREADY_DONE_RULE not in spec.prompt


def test_rule_declared_once_per_shared_block_not_per_prompt():
    from coordinator_core.ops.dispatch_emit.emit import SharedBlocks

    shared = SharedBlocks()
    specs = predispatch.check_specs([_row(f"p.C{i}") for i in range(4)])
    resolved = [
        s.prompt.replace(
            predispatch.ALREADY_DONE_RULE_MARKER, shared.ref(predispatch.ALREADY_DONE_RULE)
        )
        for s in specs
    ]
    assert all("${_shared[0]}" in p for p in resolved)
    assert shared.declaration().count("is never evidence") == 1


def test_no_existence_only_guidance():
    (spec,) = predispatch.check_specs([_row("p.C1")])
    assert "exists" not in spec.prompt.lower()
    rule = predispatch.ALREADY_DONE_RULE
    assert "never evidence" in rule
    assert "still-open" in rule


def test_undeclared_writes_rendered():
    row = _row("p.C1")._replace(writes=object())
    (spec,) = predispatch.check_specs([row])
    assert "(undeclared)" in spec.prompt
