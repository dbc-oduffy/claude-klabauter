"""The judge and falsifier schemas carry optional failing node ids, and the judge prompt asks for them."""

import json

from coordinator_core.ops.dispatch_emit.emit import _falsifier_schema_literal
from coordinator_core.ops.review_mint.execute_review import (
    _criterion_judge_prompt,
    _widen_judge_schema,
)
from coordinator_core.ops.review_mint.tests.test_execute_review import _judge_review
from coordinator_core.ops.review_test_blocker import FAILING_DETAIL_FIELD


def _assert_optional_failing(schema):
    props = schema["properties"]
    assert props["failing"]["items"] == {"type": "string"}
    item = props[FAILING_DETAIL_FIELD]["items"]
    assert item["required"] == ["node_id", "message"]
    assert item["properties"]["message"]["maxLength"] == 300
    assert "failing" not in schema.get("required", [])
    assert FAILING_DETAIL_FIELD not in schema.get("required", [])


def test_widened_judge_schema_carries_failing():
    _assert_optional_failing(json.loads(_widen_judge_schema(json.dumps({"type": "object", "properties": {}}))))


def test_falsifier_schema_carries_failing():
    _assert_optional_failing(json.loads(_falsifier_schema_literal()))


_LONG = "x" * 450


def _result():
    return {
        "differs_from_baseline": False,
        "baseline_output": "",
        "clauses": [{"clause": _LONG, "status": "met"}],
        "withheld_clauses": [{"clause": _LONG, "rows": ["C1"]}],
    }


def _validate_long_clause(schema):
    import jsonschema

    props = {k: v for k, v in schema["properties"].items() if k in _result()}
    jsonschema.validate(_result(), {"type": "object", "properties": props})


def test_widened_judge_schema_accepts_a_clause_over_200_chars():
    _validate_long_clause(json.loads(_widen_judge_schema(json.dumps({"type": "object", "properties": {}}))))


def test_widened_judge_schema_unbounds_a_clause_the_roster_schema_capped():
    capped = {"type": "object", "properties": {"clauses": {"type": "array", "items": {
        "type": "object", "properties": {"clause": {"type": "string", "maxLength": 200}}}}}}
    _validate_long_clause(json.loads(_widen_judge_schema(json.dumps(capped))))


def test_falsifier_schema_accepts_a_clause_over_200_chars():
    _validate_long_clause(json.loads(_falsifier_schema_literal()))


def test_judge_prompt_asks_for_failing_node_ids():
    _, prompt = _criterion_judge_prompt(
        _judge_review(), plan_path="docs/plans/example.md", run_base_sha="a" * 40,
        falsifier=None, prompt_head="", criterion=None, host_degraded=False,
        evidence_path=None, evidence=None, em_owned_rows=None, ledger_charter=None,
    )
    assert "in `failing`" in prompt and FAILING_DETAIL_FIELD in prompt
