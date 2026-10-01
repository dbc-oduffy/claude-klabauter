import json

import pytest

from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import (
    RosterFragmentError,
    parse_execute_review,
)


def _v5_fragment() -> dict:
    """DoE D1's pinned example, verbatim (review-inside-execute-plan.md
    § Contract)."""
    return {
        "schema": "review-roster-fragment",
        "schema_version": 5,
        "blocking_verdicts": {
            "coordinator:code-reviewer": "BLOCKED",
            "coordinator:delivery-verifier": "FAIL",
        },
        "execute_review": {
            "brightline": {
                "op": "review_brightline_gate",
                "partition_on": "PARTITION-MANDATORY",
            },
            "stages": [
                {
                    "kind": "prep",
                    "agents": [
                        {
                            "agentType": "coordinator:test-runner",
                            "model": "haiku",
                            "effort": "low",
                            "schema": "review-prep-result",
                        }
                    ],
                },
                {
                    "kind": "review-wave",
                    "agents": [
                        {
                            "agentType": "coordinator:code-reviewer",
                            "model": "opus",
                            "effort": "low",
                            "per": "slice",
                            "applies": "own-slice",
                            "schema": "slice-review-result",
                        },
                        {
                            "agentType": "coordinator:overengineering-reviewer",
                            "model": "opus",
                            "effort": "low",
                            "per": "whole-diff",
                            "applies": "none",
                            "schema": "whole-diff-review-result",
                        },
                        {
                            "accepts_signals": "named",
                            "per": "whole-diff",
                            "applies": "none",
                            "schema": "whole-diff-review-result",
                        },
                        {
                            "agentType": "coordinator:delivery-verifier",
                            "model": "sonnet",
                            "effort": "low",
                            "per": "whole-diff",
                            "applies": "none",
                            "schema": "delivery-verdict",
                        },
                    ],
                },
                {
                    "kind": "integration",
                    "agents": [
                        {
                            "agentType": "coordinator:code-reviewer",
                            "model": "opus",
                            "effort": "low",
                            "applies": "residue",
                            "schema": "review-integration-result",
                        }
                    ],
                },
            ],
        },
    }


# Minimal stage_schemas dict with the five D1 `$defs` names (C11 body).
_STAGE_SCHEMAS = {
    "review-prep-result": {"type": "object", "properties": {"plan_id": {"type": "string"}}},
    "slice-review-result": {"type": "object", "properties": {"slice_id": {"type": "string"}}},
    "whole-diff-review-result": {"type": "object", "properties": {"verdict": {"type": "string"}}},
    "delivery-verdict": {"type": "object", "properties": {"verdict": {"type": "string"}}},
    "review-integration-result": {"type": "object", "properties": {"fixes_applied": {"type": "integer"}}},
}


def _compose():
    review = parse_execute_review(
        _v5_fragment(), signals={"named": ["coordinator:staff-eng"]}
    )
    return review, compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["coordinator_core/ops/review_mint/execute_review.py"],
        prompt_head="BRIEF PRECEDENCE CLAUSE",
    )


def test_compose_execute_review_emits_three_phases_in_order():
    _, phases = _compose()
    titles = [title for title, _ in phases]
    assert titles == ["Review prep", "Review wave", "Review integration"]


def test_prep_phase_binds_reviewprep_and_carries_plan_and_sha():
    _, phases = _compose()
    _, prep_block = phases[0]
    assert "const _reviewPrep = await agent(" in prep_block
    assert "docs/plans/example.md" in prep_block
    assert "a" * 40 in prep_block
    assert "'label: " not in prep_block  # label rendered unquoted key
    assert "label: 'review:coordinator:test-runner'" in prep_block
    assert "model: 'haiku'" in prep_block
    assert "effort: 'low'" in prep_block


def test_review_wave_is_one_parallel_with_slice_map_and_whole_diff_calls():
    _, phases = _compose()
    _, wave_block = phases[1]
    assert wave_block.count("await parallel([") == 1
    assert (
        "...(_reviewPrep?.slices ?? []).map(s => () => agent(" in wave_block
    )
    assert "JSON.stringify(s)" in wave_block
    # one whole-diff call per non-slice agent (overengineering-reviewer,
    # the signal-resolved staff-eng, delivery-verifier), plus the one
    # slice-map arrow -- 4 total `() => agent(` occurrences.
    assert wave_block.count("() => agent(") == 4
    assert "const _deliveryVerdict = _reviewWave[_reviewWave.length - 1];" in wave_block


def test_every_wave_call_carries_its_roster_model_effort_and_schema():
    _, phases = _compose()
    _, wave_block = phases[1]
    assert "agentType: 'coordinator:overengineering-reviewer', model: 'opus', effort: 'low'" in wave_block
    # delivery-verifier degrades to general-purpose unconditionally (its
    # plugin registration may lag a reload) -- the roster model/effort still
    # carry through under the substituted agentType.
    assert "agentType: 'general-purpose', model: 'sonnet', effort: 'low'" in wave_block
    # signal-resolved persona carries neither model nor effort
    assert "agentType: 'coordinator:staff-eng', schema:" in wave_block
    assert json.dumps(_STAGE_SCHEMAS["whole-diff-review-result"], sort_keys=True) in wave_block
    assert json.dumps(_STAGE_SCHEMAS["delivery-verdict"], sort_keys=True) in wave_block
    assert json.dumps(_STAGE_SCHEMAS["slice-review-result"], sort_keys=True) in wave_block


def test_integration_phase_binds_reviewintegration():
    _, phases = _compose()
    _, integration_block = phases[2]
    assert "const _reviewIntegration = await agent(" in integration_block
    assert "agentType: 'coordinator:code-reviewer'" in integration_block
    assert json.dumps(_STAGE_SCHEMAS["review-integration-result"], sort_keys=True) in integration_block


def test_integration_prompt_tells_the_agent_its_edit_is_sanctioned():
    """wf_f2892741-12c regression: the integration stage's `coordinator:
    code-reviewer` identity reported "That role cannot edit source" and
    applied 0 findings. Its Edit is genuinely sandbox-denied unless the
    run's declared paths are registered as review targets (emit.py,
    compose_script) AND the prompt itself has to tell the agent so --
    absent that, the agent's own doctrine (Edit reaches only the sidecar
    and REGISTERED targets) reads as "nothing is registered for me"."""
    _, phases = _compose()
    _, integration_block = phases[2]
    for marker in (
        "Edit on any of them is sanctioned",
        "Findings Ledger",
        "apply every outstanding finding in place",
    ):
        assert marker in integration_block, f"integration prompt missing: {marker!r}"


def test_zero_integration_stage_emits_no_integrate_dispatch():
    """2026-09-28 PM order step b': a fragment with no 'integration' stage
    composes prep + review-wave only -- no `_reviewIntegration` call, no
    third phase, at all."""
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"] = [
        s for s in fragment["execute_review"]["stages"] if s["kind"] != "integration"
    ]
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    assert review.integration is None
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["coordinator_core/ops/review_mint/execute_review.py"],
        prompt_head="BRIEF PRECEDENCE CLAUSE",
    )
    titles = [title for title, _ in phases]
    assert titles == ["Review prep", "Review wave"]
    for _, block in phases:
        assert "_reviewIntegration" not in block


def test_unknown_schema_name_raises_roster_fragment_error():
    review = parse_execute_review(_v5_fragment())
    bad_schemas = dict(_STAGE_SCHEMAS)
    del bad_schemas["review-prep-result"]
    with pytest.raises(RosterFragmentError, match="unknown schema name"):
        compose_execute_review(
            review,
            stage_schemas=bad_schemas,
            plan_path="docs/plans/example.md",
            run_base_sha="a" * 40,
            declared_paths=[],
        )


def test_delivery_verdict_offset_accounts_for_position_among_whole_diff_agents():
    # Move delivery-verifier off the end: it is now second-to-last among
    # whole-diff calls (overengineering-reviewer, delivery-verifier,
    # staff-eng-via-signal) -- the offset must still resolve correctly by
    # counting whole-diff agents only, from the array's end.
    fragment = _v5_fragment()
    stages = fragment["execute_review"]["stages"][1]["agents"]
    stages[2], stages[3] = stages[3], stages[2]  # swap delivery-verifier and accepts_signals entries
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=[],
    )
    _, wave_block = phases[1]
    assert "const _deliveryVerdict = _reviewWave[_reviewWave.length - 2];" in wave_block


def test_schema_literal_inlines_defs_refs_self_contained():
    # A per-agent schema that $refs a sibling $defs entry must come out of
    # compose_execute_review with the $ref inlined -- the emitted schema is
    # JSON-serialized standalone with no surrounding $defs to resolve
    # against at agent()-call time.
    schemas_with_ref = dict(_STAGE_SCHEMAS)
    schemas_with_ref["anchor"] = {"type": "string", "format": "uuid"}
    schemas_with_ref["delivery-verdict"] = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string"},
            "row_id": {"$ref": "#/$defs/anchor"},
        },
    }
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=schemas_with_ref,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=[],
    )
    _, wave_block = phases[1]
    assert "$ref" not in wave_block
    assert '"format": "uuid"' in wave_block


def test_delivery_verifier_degrades_to_general_purpose_unconditionally():
    # coordinator:delivery-verifier's plugin registration may lag a reload
    # independent of agent_type_host -- it must never be the literal
    # `agentType` an emitted script names.
    _, phases = _compose()
    _, wave_block = phases[1]
    assert "coordinator:delivery-verifier" not in wave_block
    assert "general-purpose" in wave_block


def test_prep_prompt_names_the_worktree_freeze_not_a_committed_range():
    # Regression: a prep told only to "characterise" froze base..HEAD (a peer's
    # commit) while the run's rows sat uncommitted, so the verifier saw no rows.
    _, phases = _compose()
    _, prep_block = phases[0]
    # The agent cannot call an op; it must be handed the runnable CLI with --worktree.
    assert "freeze-review-diff" in prep_block
    assert "--worktree" in prep_block
    assert "whole_diff_path" in prep_block


def test_whole_diff_agents_are_handed_the_frozen_diff_path_at_run_time():
    _, phases = _compose()
    _, wave_block = phases[1]
    # every whole-diff call (overengineering, staff-eng, delivery-verifier)
    assert wave_block.count("_reviewPrep?.whole_diff_path") == 3
    # only the delivery verifier is handed its sidecar
    assert wave_block.count("_reviewPrep?.whole_diff_sidecars?.delivery") == 1


def _judge_review():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].append(
        {
            "kind": "judge",
            "agents": [
                {
                    "agentType": "coordinator:criterion-judge",
                    "model": "opus",
                    "effort": "low",
                    "schema": "judge-result",
                }
            ],
        }
    )
    return parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})


def _compose_judge(stage_schemas, falsifier=None):
    from coordinator_core.ops.review_mint.execute_review import compose_criterion_judge

    return compose_criterion_judge(
        _judge_review(),
        stage_schemas=stage_schemas,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        falsifier=falsifier,
    )


def _emitted_schema(call: str) -> dict:
    start = call.index("schema: ") + len("schema: ")
    obj, _ = json.JSONDecoder().raw_decode(call[start:])
    return obj


def test_judge_schema_requires_the_boolean_and_baseline_output():
    stage_schemas = {
        **_STAGE_SCHEMAS,
        "judge-result": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    }
    schema = _emitted_schema(_compose_judge(stage_schemas))
    assert schema["properties"]["differs_from_baseline"] == {"type": "boolean"}
    assert schema["properties"]["baseline_output"] == {"type": "string", "maxLength": 300}
    assert {"status", "differs_from_baseline", "baseline_output"} <= set(schema["required"])
    assert "status" in schema["properties"]


def test_judge_schema_widening_tolerates_a_bare_object_schema():
    schema = _emitted_schema(_compose_judge({**_STAGE_SCHEMAS, "judge-result": {"type": "object"}}))
    assert schema["properties"]["differs_from_baseline"] == {"type": "boolean"}
    assert set(schema["required"]) == {"differs_from_baseline", "baseline_output"}


def test_judge_prompt_asks_for_the_boolean_comparison():
    call = _compose_judge(
        {**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        falsifier={"how": "run it", "baseline_output": "fails", "expected_when_true": "passes"},
    )
    assert "differs_from_baseline" in call and "baseline_output" in call
