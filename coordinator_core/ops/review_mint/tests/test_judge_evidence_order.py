"""The terminal judge's brief states a ranked evidence order and inlines register evidence verbatim."""
from __future__ import annotations

import json

from coordinator_core.ops.requirement_register import JudgeEvidence
from coordinator_core.ops.review_mint.execute_review import (
    _prompt_literal,
    compose_criterion_judge,
)
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.review_mint.tests.test_execute_review import (
    _STAGE_SCHEMAS,
    _judge_review,
)


def _call(evidence=None):
    return compose_criterion_judge(
        _judge_review(),
        stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        falsifier=None,
        evidence=evidence,
    )


def test_tiers_appear_in_rank_order():
    call = _call()
    assert call.index("TIER 1") < call.index("TIER 2") < call.index("TIER 3")


def test_no_evidence_leaves_pointer_tier():
    call = _call()
    assert "TIER 1 (pointer)" in call
    assert "TIER 1 evidence, verbatim" not in call


def test_row_text_reaches_the_prompt_byte_identical():
    text = ("it's \"quoted\"\nsecond line\\n literal " + "x" * 2000)
    ev = JudgeEvidence(
        rows=[{"id": "r1", "source_text": text, "anchor": "p.3", "surface": "ui"}],
        pm_words=[("intent", "PM said: ship 'it'\nnow")],
        rulings=[("apm", "r1", "defer until Q3")],
    )
    call = _call(ev)
    assert _prompt_literal_fragment(text) in call
    assert _prompt_literal_fragment("PM said: ship 'it'\nnow") in call
    assert "ruling by the APM on row r1" in call
    assert "anchor=p.3" in call and "surface=ui" in call
    assert call.index("TIER 1, the human") < call.index("TIER 2, the exit") < call.index("TIER 3")


def _prompt_literal_fragment(value: str) -> str:
    return _prompt_literal(value)[1:-1]


def test_widened_schema_carries_register_rows_optionally():
    call = _call()
    start = call.index("schema: ") + len("schema: ")
    schema, _ = json.JSONDecoder().raw_decode(call[start:])
    item = schema["properties"]["register_rows"]["items"]
    assert set(item["required"]) == {"id", "claim", "status", "wired"}
    assert set(item["properties"]) >= {"surface", "observed_ref", "ruling_ref"}
    assert "register_rows" not in schema.get("required", [])


def test_plugin_register_rows_shape_is_kept():
    own = {"type": "array", "items": {"type": "object", "title": "doe"}}
    call = compose_criterion_judge(
        _judge_review(),
        stage_schemas={**_STAGE_SCHEMAS, "judge-result": {
            "type": "object", "properties": {"register_rows": own}}},
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        falsifier=None,
    )
    start = call.index("schema: ") + len("schema: ")
    schema, _ = json.JSONDecoder().raw_decode(call[start:])
    assert schema["properties"]["register_rows"] == own


def test_the_judge_judges_the_uncommitted_tree_as_the_state_to_commit():
    # The judge runs before dispatch.terminal_commit; an untracked deliverable is expected.
    from coordinator_core.ops.review_mint.execute_review import _JUDGE_PREAMBLE

    assert "uncommitted by design" in _JUDGE_PREAMBLE
    assert "never a reason for not_met" in _JUDGE_PREAMBLE
