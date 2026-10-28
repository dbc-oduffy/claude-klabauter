import re
import json

import pytest

from coordinator_core.ops.review_mint.execute_review import (
    _PREP_BUSY_RETRIES,
    _wave_schema_literal,
    PREP_MALFORMED_HALT,
    PREP_BUSY_HALT,
    REVIEW_MISSING_HALT,
    SLICE_ID_UNKNOWN_HALT,
    compose_execute_review,
    prep_slice_id_for,
)
from coordinator_core.ops.review_prep import REVIEW_PREP_BUSY_PREFIX
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
                            "schema": "delivery-verdict-result",
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
    "delivery-verdict-result": {"type": "object", "properties": {"verdict": {"type": "string"}}},
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
    assert "const _reviewPrep = await (async () => { try { return await agent(" in prep_block
    assert "docs/plans/example.md" in prep_block
    assert "a" * 40 in prep_block
    assert "'label: " not in prep_block  # label rendered unquoted key
    assert "label: 'review:coordinator:test-runner'" in prep_block
    assert "model: 'haiku'" in prep_block
    assert "effort: 'low'" in prep_block


def test_prep_phase_fails_closed_on_no_prep_or_no_slices():
    _, phases = _compose()
    _, prep_block = phases[0]
    # The halt guard is no longer the block's last line: the excluded-paths guard follows it.
    guard = prep_block.splitlines()[-3]
    assert guard.startswith("  if (!_reviewPrep || (!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).length && !_verifyOnly))")
    assert "product_files" not in guard
    assert "throw" not in guard and "return { halted:" in guard


def test_review_wave_is_one_parallel_with_slice_map_and_whole_diff_calls():
    _, phases = _compose()
    _, wave_block = phases[1]
    assert wave_block.count("await parallel([") == 1
    assert (
        "...(Array.isArray(_reviewPrep?.slices) ? _reviewPrep.slices : []).map(s => () => (async () => { try { return await agent(" in wave_block
    )
    assert "JSON.stringify(s)" in wave_block
    # one whole-diff call per non-slice agent (overengineering-reviewer,
    # the signal-resolved staff-eng, delivery-verifier), plus the one
    # slice-map arrow -- 4 total `() => agent(` occurrences.
    assert wave_block.count("() => (async () => { try { return await agent(") == 4
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
    assert json.dumps(_STAGE_SCHEMAS["delivery-verdict-result"], sort_keys=True) in wave_block
    assert json.dumps(_STAGE_SCHEMAS["slice-review-result"], sort_keys=True) in wave_block


def test_integration_phase_binds_reviewintegration():
    _, phases = _compose()
    _, integration_block = phases[2]
    assert "const _reviewIntegration = _verifyOnly ? null : await (async () => { try { return await agent(" in integration_block
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
    schemas_with_ref["delivery-verdict-result"] = {
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


def _compose_prep(**extra) -> str:
    review = parse_execute_review(
        _v5_fragment(), signals={"named": ["coordinator:staff-eng"]}
    )
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["coordinator_core/ops/review_mint/execute_review.py"],
        prompt_head="BRIEF PRECEDENCE CLAUSE",
        **extra,
    )
    return "\n".join(block for _, block in phases)


def test_review_only_prep_freezes_and_partitions_the_committed_range_never_the_worktree():
    sha = "b" * 40
    prep = _compose_prep(review_terminal_sha=sha, precredited_rows=["C0"])
    assert f"freeze-review-diff --range {'a' * 40}..{sha} --slice-id" in prep
    assert "--worktree" not in prep
    assert "UNCOMMITTED" not in prep
    assert '\\"worktree\\"' not in prep and '"worktree"' not in prep
    assert f'\\"head\\":\\"{sha}\\"' in prep or f'"head":"{sha}"' in prep
    assert f"{'a' * 40}..{sha} also changed" in prep
    assert "..HEAD" not in prep


def test_prep_without_terminal_sha_keeps_the_worktree_prompt():
    prep = _compose_prep(precredited_rows=["C0"])
    assert "freeze-review-diff --worktree --range " + "a" * 40 + " --slice-id" in prep
    assert "worktree" in prep and "land UNCOMMITTED" in prep
    assert f"{'a' * 40}..HEAD also changed" in prep
    assert "head" not in prep.split("review.partition_slices", 1)[1].split("its ", 1)[0]
    assert "merge-base --is-ancestor" not in prep


def test_ancestry_rule_appears_only_in_the_review_only_prompt():
    review_only = _compose_prep(review_terminal_sha="b" * 40, precredited_rows=["C0"])
    normal = _compose_prep(precredited_rows=["C0"])
    assert "git merge-base --is-ancestor <sha> <base>" in review_only
    assert "any other exit is unknown" in review_only
    assert "merge-base" not in normal


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


def _tiered_review():
    fragment = _v5_fragment()
    agents = fragment["execute_review"]["stages"][1]["agents"]
    agents[1]["tiers"] = ["standard", "full"]
    agents[3]["tiers"] = ["standard", "full"]
    return parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})


def _wave_block(review, **kw) -> str:
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        **kw,
    )
    return dict(phases)["Review wave"]


def test_lightweight_tier_drops_tiered_agents_and_nulls_the_delivery_verdict():
    block = _wave_block(_tiered_review(), tier="lightweight")
    assert "coordinator:code-reviewer" in block
    assert "coordinator:overengineering-reviewer" not in block
    assert "agent(" in block and block.count("delivery-verifier") == 1
    assert "const _deliveryVerdict = null;" in block
    assert "coordinator:staff-eng" in block


@pytest.mark.parametrize("tier", ["standard", "full"])
def test_higher_tiers_keep_the_full_wave(tier):
    review = _tiered_review()
    assert _wave_block(review, tier=tier) == _wave_block(review)
    assert "const _deliveryVerdict = _reviewWave[_reviewWave.length - 1];" in _wave_block(review, tier=tier)


def test_fragment_without_tiers_is_byte_identical_for_any_tier():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    base = _wave_block(review)
    assert _wave_block(review, tier="lightweight") == base


def test_tier_js_gates_agents_at_run_time_and_keeps_the_verdict_offset():
    block = _wave_block(_tiered_review(), tier_js="_tier")
    assert '["standard", "full"].includes(_tier)' in block
    assert "const _deliveryVerdict = _reviewWave[_reviewWave.length - 1];" in block
    assert "coordinator:overengineering-reviewer" in block


def test_compile_tier_exclusion_binds_the_delivery_reason_literal():
    block = _wave_block(_tiered_review(), tier="lightweight")
    assert "const _deliveryNotRun = 'tier lightweight excludes coordinator:delivery-verifier';" in block


def test_delivery_reason_binds_on_the_tier_skip_rejection_and_empty_paths():
    block = _wave_block(_tiered_review(), tier_js="_tier")
    assert "let _deliveryNotRunReason = null;" in block
    assert (
        "(_deliveryNotRunReason = 'tier ' + String(_tier) + ' excludes coordinator:delivery-verifier', null)"
        in block
    )
    assert "catch (e) { _deliveryNotRunReason = 'rejected: ' + String(e && e.message || e); return null; }" in block
    assert (
        "const _deliveryNotRun = _deliveryVerdict ? null : (_deliveryNotRunReason ?? 'returned no result');"
        in block
    )
    assert "let _deliveryNotRun " not in block and "let _deliveryNotRun;" not in block
    assert block.count("_deliveryNotRunReason = ") == 3  # the declaration plus two writers


def test_only_the_delivery_verifier_carries_the_reason_hook():
    block = _wave_block(_tiered_review(), tier_js="_tier")
    assert block.count("catch (e) { _deliveryNotRunReason") == 1
    assert block.count("_deliveryNotRunReason = 'rejected: '") == 1


def _persona_review(**kw):
    return parse_execute_review(
        _v5_fragment(),
        signals={"named": []},
        required_reviewers=["coordinator:senior-front-end"],
        **kw,
    )


def _not_run_catches(block: str) -> list:
    return re.findall(r"catch \(e\) \{ (_waveNotRun\.push\(.*?\);) return null; \}", block)


def test_every_non_delivery_whole_diff_slot_catch_records_a_rejected_reason():
    review = _persona_review()
    block = _wave_block(review)
    whole = [a for a in review.review_wave if a.per != "slice" and a.agent_type != "coordinator:delivery-verifier"]
    catches = _not_run_catches(block)
    assert len(whole) >= 2 and len(catches) == len(whole)
    persona = [c for c in catches if "'coordinator:senior-front-end'" in c]
    assert len(persona) == 1
    assert "reason: 'rejected: ' + String(e && e.message || e)" in persona[0]
    assert block.count("catch (e) { _deliveryNotRunReason") == 1


def test_a_gated_slot_records_skipped_with_the_tier():
    block = _wave_block(_tiered_review(), tier_js="_tier")
    assert (
        "_waveNotRun.push({ slot: -3, agent_type: 'coordinator:overengineering-reviewer', "
        "reason: 'skipped: tier ' + String(_tier) + ' excludes coordinator:overengineering-reviewer' }), null)"
    ) in block


def test_a_tshirt_gated_slot_skip_reason_names_the_size_not_the_tier():
    block = _wave_block(_ratio_review(kira_min="L"), executor_rows=4, tshirt_js="'M'", tier_js="_tier")
    assert (
        "reason: 'skipped: ' + 'size ' + String('M') + ' excludes coordinator:overengineering-reviewer' }), null)"
    ) in block
    assert "'skipped: tier ' + String(_tier) + ' excludes coordinator:overengineering-reviewer'" not in block


def test_a_slot_gated_by_tier_and_tshirt_picks_the_excluding_gate_at_fire_time():
    fragment = _ratio_fragment(kira_min="L")
    fragment["execute_review"]["stages"][1]["agents"][1]["tiers"] = ["standard", "full"]
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    block = _wave_block(review, executor_rows=4, tshirt_js="'M'", tier_js="_tier")
    assert (
        "reason: 'skipped: ' + ([\"standard\", \"full\"].includes(_tier) ? 'size ' + String('M') : "
        "'tier ' + String(_tier)) + ' excludes coordinator:overengineering-reviewer' }), null)"
    ) in block


def test_verify_only_skip_records_skipped_verify_only():
    block = _wave_block(_persona_review())
    assert block.count("reason: 'skipped: verify-only' }), null)") == 2


def test_post_wave_sweep_records_returned_no_review_and_resolves_slot_indexes():
    block = _wave_block(_persona_review())
    sweeps = [l for l in block.splitlines() if "returned no review" in l and l.lstrip().startswith("if (!_reviewWave[_reviewWave.length")]
    assert len(sweeps) == 2
    assert all("!_reviewWave[_reviewWave.length - " in l and ".some(n => n.slot === " in l for l in sweeps)
    assert block.index("returned no review") > block.index("const _reviewWave")
    assert "_waveNotRun.forEach(n => { n.slot += _reviewWave.length; });" in block
    assert "let _waveNotRun" not in block and "const _waveNotRun" not in block


def test_prep_in_wave_prep_alone_reply_is_swept_before_the_prep_guards():
    wave = dict(_in_wave())["Review wave"]
    assert wave.index("returned no review") < wave.index("_reviewPrep")
    assert wave.count("returned no review") == 3


def test_tier_and_tier_js_together_or_unknown_tier_raise():
    review = _tiered_review()
    with pytest.raises(ValueError):
        _wave_block(review, tier="full", tier_js="_t")
    with pytest.raises(ValueError):
        _wave_block(review, tier="huge")


def test_judge_folds_in_the_ledger_charter_and_fields():
    from coordinator_core.ops.review_mint.execute_review import compose_criterion_judge

    ledger_schema = {
        "required": ["items"],
        "properties": {
            "items": {"type": "array"},
            "all_of_pm_ask": {"type": "boolean"},
            "conflicts": {"type": "array"},
        },
    }
    call = compose_criterion_judge(
        _judge_review(),
        stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        falsifier=None,
        ledger_charter="LEDGER CHARTER TEXT",
        ledger_schema=ledger_schema,
    )
    schema = _emitted_schema(call)
    assert "LEDGER CHARTER TEXT" in call
    assert {"items", "all_of_pm_ask", "conflicts", "differs_from_baseline"} <= set(schema["properties"])
    assert {"items", "differs_from_baseline"} <= set(schema["required"])


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


def test_judge_prompt_treats_landed_sha_and_sha_memo_clauses_as_post_commit_acts():
    call = _compose_judge({**_STAGE_SCHEMAS, "judge-result": {"type": "object"}})
    assert "never a reason for not_met" in call
    assert "landed sha" in call and "a memo naming it" in call
    assert "post-commit act" in call


def test_judge_prompt_and_schema_carry_completion_summary_and_lesson_candidates():
    call = _compose_judge({**_STAGE_SCHEMAS, "judge-result": {"type": "object"}})
    assert "`completion_summary`" in call and "`lesson_candidates`" in call
    assert "authored_by: terminal-judge" in call
    props = _emitted_schema(call)["properties"]
    assert props["completion_summary"] == {"type": "string", "maxLength": 600}
    assert props["authored_by"] == {"type": "string", "enum": ["terminal-judge"]}
    item = props["lesson_candidates"]["items"]
    assert set(item["required"]) == {"title", "body", "change_kind", "target_wiki"}
    assert item["additionalProperties"] is False


def test_judge_schema_keeps_doe_s_own_completion_fields_when_present():
    doe = {"type": "string", "maxLength": 600, "description": "DoE's"}
    doe_lessons = {"type": "array", "items": {"type": "object"}, "description": "DoE's"}
    doe_author = {"type": "string", "enum": ["terminal-judge"], "description": "DoE's"}
    judge = {
        "type": "object",
        "properties": {
            "completion_summary": doe,
            "lesson_candidates": doe_lessons,
            "authored_by": doe_author,
        },
    }
    props = _emitted_schema(_compose_judge({**_STAGE_SCHEMAS, "judge-result": judge}))["properties"]
    assert props["completion_summary"] == doe
    assert props["lesson_candidates"] == doe_lessons
    assert props["authored_by"] == doe_author


def test_every_review_phase_call_degrades_to_null_not_a_throw():
    blocks = dict(_compose()[1])
    for title in ("Review prep", "Review wave", "Review integration"):
        assert "catch (e)" in blocks[title], title
        assert "catch (e) { return null; }" in blocks[title], title


def _compose_with(**paths):
    review = parse_execute_review(
        _v5_fragment(), signals={"named": ["coordinator:staff-eng"]}
    )
    return compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        **paths,
    )


def test_declared_paths_js_is_spliced_into_the_prep_prompt_at_run_time():
    phases = _compose_with(declared_paths_js="_declared")
    prep_block = phases[0][1]
    assert "JSON.stringify(_declared)" in prep_block
    assert "declared_paths:' + ' ' + JSON.stringify(_declared)" in prep_block


def test_declared_paths_and_declared_paths_js_together_or_neither_raise():
    with pytest.raises(ValueError):
        _compose_with(declared_paths=["a.py"], declared_paths_js="_declared")
    with pytest.raises(ValueError):
        _compose_with()


def test_literal_declared_paths_path_is_unchanged():
    prep_block = _compose_with(declared_paths=["a.py", "b.py"])[0][1]
    assert "declared_paths: a.py, b.py" in prep_block
    assert "JSON.stringify(_declared" not in prep_block  # no run-time splice


def test_prep_scopes_foreign_claims_to_peer_commits_inside_the_footprint():
    _, phases = _compose()
    _, prep_block = phases[0]
    assert "foreign_claims list ONLY declared paths" in prep_block
    assert "NOT in declared_paths under foreign_claims" not in prep_block


def _run_prep_block(prep_result: dict, landed: dict | None = None) -> dict:
    """Execute the emitted prep block under node with the agent call stubbed to
    return ``prep_result``; returns ``{"slices": [...]}`` or ``{"error": msg}``."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    _, phases = _compose()
    lines = phases[0][1].splitlines()
    body = [ln for ln in lines if not ln.lstrip().startswith("phase(")]
    body = [
        f"  const _reviewPrep = {json.dumps(prep_result)};"
        if ln.startswith("  const _reviewPrep = await")
        else ln
        for ln in body
    ]
    script = (
        "(async () => { try { const _out = await (async () => {\n"
        + (f"const _landed = {json.dumps(landed)};\n" if landed is not None else "")
        + "\n".join(body)
        + "\n return {slices: _reviewPrep.slices, verifyOnly: _verifyOnly}; })();"
        "\n console.log(JSON.stringify(_out));"
        "\n} catch (e) { console.log(JSON.stringify({error: e.message})); } })();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


_PREP = {
    "verdict": "single-reviewer-ok",
    "product_files": 27,
    "slices": [],
    "whole_diff_path": "state/share/whole.diff",
    "whole_diff_sidecars": {"kira": "k.md", "personas": ["p.md"], "delivery": "d.md"},
}


def test_single_reviewer_ok_with_product_files_yields_exactly_one_whole_diff_slice():
    result = _run_prep_block(dict(_PREP))
    assert len(result["slices"]) == 1
    assert result["slices"][0]["diff_path"] == "state/share/whole.diff"
    assert result["slices"][0]["sidecar_path"] == "p.md"


@pytest.mark.parametrize(
    "prep",
    [
        {**_PREP, "verdict": "PARTITION-MANDATORY"},
        {**_PREP, "verdict": "PARTITION-MANDATORY", "product_files": 0},
        {**_PREP, "product_files": 0, "foreign_claims": ["peer.py"]},
        None,
    ],
)
def test_empty_slices_without_single_reviewer_ok_over_product_files_still_refuses(prep):
    result = _run_prep_block(prep)
    assert result["halted"].startswith("review prep returned no slices")
    assert "error" not in result and result["wave"] is None


def test_prep_prompt_says_single_reviewer_ok_returns_one_whole_diff_slice():
    _, phases = _compose()
    assert "return exactly ONE slice spanning every product file" in phases[0][1]


def test_single_reviewer_ok_over_no_product_file_is_a_no_op_not_a_refusal():
    # The C9 live --ask run's one row found its edit already made: prep froze
    # an empty diff cleanly, and the run threw instead of finishing.
    result = _run_prep_block({**_PREP, "product_files": 0})
    assert result["halted"] == "no-op"
    assert "error" not in result


_ZERO_FILE_PREP = {
    "verdict": None,
    "basis": "worktree",
    "product_files": 0,
    "foreign_claims": [],
    "slices": [],
    "whole_diff_path": None,
    "error": None,
}


def test_zero_file_prep_with_no_error_is_nothing_to_review_not_a_refusal():
    assert _run_prep_block(dict(_ZERO_FILE_PREP))["halted"] == "no-op"
    result = _run_prep_block(dict(_ZERO_FILE_PREP), landed=_LANDED)
    assert "halted" not in result and result["verifyOnly"] is True


@pytest.mark.parametrize(
    "prep",
    [
        {**_ZERO_FILE_PREP, "error": "freeze failed"},
        {**_ZERO_FILE_PREP, "basis": ""},
        {**_ZERO_FILE_PREP, "verdict": "PARTITION-MANDATORY"},
        {**_ZERO_FILE_PREP, "foreign_claims": ["peer.py"]},
    ],
)
def test_zero_file_prep_that_errored_or_refused_still_halts(prep):
    for landed in (None, _LANDED):
        assert _run_prep_block(prep, landed=landed)["halted"].startswith("review prep returned no slices")


def test_zero_product_files_disagreeing_with_a_slice_that_lists_files_does_not_halt():
    prep = {**_PREP, "product_files": 0, "slices": [{"id": "s1", "files": ["x.py"], "diff_path": "d"}]}
    result = _run_prep_block(prep)
    assert "halted" not in result and result["slices"][0]["id"] == "s1"


_LANDED = {"C12": {"status": "coded"}}


def test_landed_rows_with_no_product_file_proceed_verification_only_not_refuse():
    result = _run_prep_block({**_PREP, "product_files": 0}, landed=_LANDED)
    assert "error" not in result and "halted" not in result
    assert result["verifyOnly"] is True and result["slices"] == []


@pytest.mark.parametrize(
    "prep",
    [
        {**_PREP, "verdict": "PARTITION-MANDATORY", "product_files": 0},
        {**_PREP, "product_files": 0, "foreign_claims": ["peer.py"]},
        {**_PREP, "verdict": "PARTITION-MANDATORY", "product_files": 3},
        None,
    ],
)
def test_landed_rows_with_failed_or_product_prep_still_refuse(prep):
    result = _run_prep_block(prep, landed=_LANDED)
    assert result["halted"].startswith("review prep returned no slices")
    assert "error" not in result and result["wave"] is None


def test_landed_rows_with_product_files_keep_their_review_slice():
    result = _run_prep_block(dict(_PREP), landed=_LANDED)
    assert result["verifyOnly"] is False and len(result["slices"]) == 1


def test_verification_only_skips_whole_diff_reviewers_and_integration_but_not_delivery_verifier():
    _, phases = _compose()
    wave_block, integration_block = phases[1][1], phases[2][1]
    assert wave_block.count("() => _verifyOnly ? (_waveNotRun.push(") == 2
    verifier = wave_block.split("const _reviewWave")[1].rsplit("() => (async", 1)[1]
    assert "_verifyOnly" not in verifier
    assert "const _reviewIntegration = _verifyOnly ? null : await" in integration_block


def test_prep_prompt_takes_product_files_from_the_launcher_line():
    _, phases = _compose()
    assert "`product_files: N` line the launcher prints on stderr" in phases[0][1]


def test_prep_never_counts_the_runs_own_writes_as_foreign_claims():
    _, phases = _compose()
    _, prep_block = phases[0]
    assert "any write by this run itself" in prep_block
    assert ".coordinator-local/subagent-share/" in prep_block


def test_host_degraded_review_wave_emits_no_coordinator_agent_type_and_keeps_every_stage():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    kwargs = dict(
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["coordinator_core/ops/review_mint/execute_review.py"],
        prompt_head="BRIEF PRECEDENCE CLAUSE",
    )
    normal = compose_execute_review(review, **kwargs)
    degraded = compose_execute_review(review, host_degraded=True, **kwargs)

    assert [t for t, _ in degraded] == [t for t, _ in normal]
    text = "\n".join(b for _, b in degraded)
    assert not re.search(r"agentType:\s*['\"]coordinator:", text)
    assert "agentType: 'general-purpose'" in text
    assert "You are acting as the " in text
    assert re.search(r"agentType:\s*['\"]coordinator:", "\n".join(b for _, b in normal))


def test_halt_names_the_prep_verdict():
    result = _run_prep_block({**_PREP, "verdict": "PARTITION-MANDATORY"})
    assert result["halted"] == "review prep returned no slices (prep verdict: PARTITION-MANDATORY)"
    assert result["prep"]["verdict"] == "PARTITION-MANDATORY"


def test_halt_names_the_excluded_paths_and_keeps_the_prefix():
    excluded = [{"path": "a.py", "reason": "binary"}, {"path": "b.py", "reason": "peer-held"}]
    result = _run_prep_block({**_PREP, "verdict": "none", "excluded": excluded})
    assert result["halted"] == (
        "review prep returned no slices (prep verdict: none); excluded: a.py: binary, b.py: peer-held"
    )


_SLICED_PREP = {**_PREP, "verdict": "PARTITION-MANDATORY", "slices": [{"id": "s1", "files": ["a.py"]}]}


def test_non_empty_slices_with_unsliceable_excluded_path_halts_naming_each():
    excluded = [
        {"path": "x.py", "reason": "outside-repo"},
        {"path": "y.py", "reason": "freeze-failed: boom"},
        {"path": "z.py", "reason": "no-delta"},
    ]
    result = _run_prep_block({**_SLICED_PREP, "excluded": excluded})
    assert result["halted"] == (
        "review prep excluded declared paths (excluded: x.py: outside-repo, y.py: freeze-failed: boom)"
    )
    assert result["prep"]["slices"]


def test_empty_or_no_delta_only_excluded_does_not_halt():
    for excluded in ([], [{"path": "z.py", "reason": "no-delta"}]):
        result = _run_prep_block({**_SLICED_PREP, "excluded": excluded})
        assert "halted" not in result
        assert result["slices"]


def test_prep_brief_runs_the_partition_op_and_carries_no_grouping_procedure():
    prep = _compose()[1][0][1]
    assert "session-claim-cli who-claims-path" in prep
    assert "coordinator-invoke" in prep and "review.partition_slices" in prep
    for gone in ("first two path segments", "merge groups", "review-brightline-gate", "that group"):
        assert gone not in prep


def _wave_prompts(product_files, *, whole=True) -> list:
    """Run the emitted review-wave block under node with agent stubbed; returns every prompt."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    _, phases = _compose()
    prep = {
        "verdict": "single-reviewer-ok",
        "product_files": product_files,
        "slices": [{"id": "s1", "files": ["a.py"], "diff_path": "d/slice.diff", "sidecar_path": "s.md"}],
        "whole_diff_path": "d/whole.diff",
        "whole_diff_sidecars": {"kira": "k.md", "personas": ["p.md"], "delivery": "d.md"},
    }
    body = "\n".join(ln for ln in phases[1][1].splitlines() if not ln.lstrip().startswith("phase("))
    script = (
        f"const _reviewPrep = {json.dumps(prep)}; const _verifyOnly = false; const _out = [];\n"
        "const agent = async (p) => { _out.push(p); return null; };\n"
        "const parallel = (fns) => Promise.all(fns.map(f => f()));\n"
        f"(async () => {{ {body}\n console.log(JSON.stringify(_out)); }})();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def test_one_file_review_briefs_carry_the_diff_only_clause_and_the_diff_path():
    prompts = _wave_prompts(1)
    assert len(prompts) >= 3
    for p in prompts:
        assert "Small diff: read only the frozen diff" in p
        assert "Read only the frozen diff named by whole_diff_path" not in p
        assert "d/slice.diff" in p or "d/whole.diff" in p


def test_ten_file_review_briefs_are_the_full_briefs():
    prompts = _wave_prompts(10)
    assert len(prompts) >= 3
    for p in prompts:
        assert "Small diff" not in p
        assert "plan_path: docs/plans/example.md" in p


def test_three_files_is_small_and_four_is_not():
    assert all("Small diff" in p for p in _wave_prompts(3))
    assert not any("Small diff" in p for p in _wave_prompts(4))


def test_small_diff_keeps_kira_and_delivery_verifier_in_the_wave():
    small, large = _wave_prompts(1), _wave_prompts(10)
    assert len(small) == len(large)
    assert any("delivery-verifier" in p for p in small)


_OWNER_ROWS = [
    {"id": "C9", "owner_repo": "coordinator-content-repo", "closure_key": "memo-1"},
    {"id": "C8", "owner_repo": "ghost-repo"},
]
_OWNER_ROOTS = {"coordinator-content-repo": "/roots/doe"}


def test_owner_repo_clause_names_registered_root_and_unregistered_not_met():
    from coordinator_core.ops.review_mint.execute_review import owner_repo_clause

    text = owner_repo_clause(_OWNER_ROWS, _OWNER_ROOTS)
    assert "C9: owned by coordinator-content-repo, root /roots/doe (closure_key memo-1" in text
    assert "at its HEAD" in text
    assert "C8: owner ghost-repo is not on this machine" in text
    assert "never report not_met for its absence" in text


def test_owner_repo_clause_empty_rows_render_nothing():
    from coordinator_core.ops.review_mint.execute_review import (
        owner_repo_clause,
        owner_repo_clause_js,
    )

    assert owner_repo_clause([], _OWNER_ROOTS) == ""
    assert owner_repo_clause_js("[]").endswith("([])")


def test_owner_repo_clause_js_carries_the_same_text_fragments():
    from coordinator_core.ops import workflow_scaffold as ws
    from coordinator_core.ops.review_mint import execute_review as er

    js = er.owner_repo_clause_js("_rows")
    for frag in (er._OWNER_HEAD, er._OWNER_OWNED, er._OWNER_ABSENT, er._OWNER_CLOSURE):
        assert ws._js_string_literal(frag) in js
    assert js.endswith("(_rows)")
    assert "r.owner_root" in js


_ENGINE_ROWS = [
    {"id": "T8", "owner_repo": "claude-klabauter", "owner_root": "/roots/engine",
     "root_class": "resolved-engine", "engine": True},
]


def test_engine_row_renders_under_its_own_head_with_root_class_and_root_read():
    from coordinator_core.ops.review_mint.execute_review import owner_repo_clause

    text = owner_repo_clause(_ENGINE_ROWS, {})
    assert "Engine-plane rows" in text and "Owner-repo rows" not in text
    assert "T8: engine-plane row, engine root /roots/engine (resolved-engine)" in text
    assert "`git -C /roots/engine` at its HEAD" in text
    assert "root_read" in text and "roots_read" in text


def test_engine_row_with_no_root_renders_the_absent_text():
    from coordinator_core.ops.review_mint.execute_review import owner_repo_clause

    text = owner_repo_clause([{**_ENGINE_ROWS[0], "owner_root": ""}], {})
    assert "T8: owner claude-klabauter is not on this machine" in text
    assert "engine root" not in text


def test_gated_only_clause_is_byte_identical_and_mixed_rows_render_both_heads():
    from coordinator_core.ops.review_mint import execute_review as er

    gated_only = er.owner_repo_clause(_OWNER_ROWS, _OWNER_ROOTS)
    assert gated_only.startswith(er._OWNER_HEAD) and er._ENGINE_HEAD not in gated_only
    mixed = er.owner_repo_clause([*_OWNER_ROWS, *_ENGINE_ROWS], _OWNER_ROOTS)
    assert mixed.startswith(gated_only)
    assert mixed.endswith(er.owner_repo_clause(_ENGINE_ROWS, {}))


def test_owner_repo_clause_js_mirrors_the_engine_fragments():
    from coordinator_core.ops import workflow_scaffold as ws
    from coordinator_core.ops.review_mint import execute_review as er

    js = er.owner_repo_clause_js("_rows")
    for frag in (er._ENGINE_HEAD, er._ENGINE_OWNED):
        assert ws._js_string_literal(frag) in js
    assert "r.engine" in js and "r.root_class" in js


def test_judge_schema_carries_roots_read():
    schema = _emitted_schema(_compose_judge({**_STAGE_SCHEMAS, "judge-result": {"type": "object"}}))
    item = schema["properties"]["roots_read"]["items"]
    assert set(item["required"]) == {"id", "root"}
    assert "roots_read" not in schema["required"]


def test_owner_repo_roots_maps_registry_keys_to_dashed_shortnames(monkeypatch):
    from coordinator_core import machine_resolver
    from coordinator_core.ops.review_mint.execute_review import owner_repo_roots

    monkeypatch.setattr(
        machine_resolver,
        "merged_flat_registry",
        lambda: {"repos.project_rag": "\\roots\\rag", "repos.empty": "", "coordinator.machine_slug": "m"},
    )
    assert owner_repo_roots() == {"project-rag": "/roots/rag"}


def _delivery_and_review_prompts(**kw):
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        **kw,
    )
    return {t: b for t, b in phases}


def test_delivery_prompt_carries_owner_clause_but_prep_and_reviewers_do_not():
    with_rows = _delivery_and_review_prompts(owner_rows_js="_ownerRows")
    without = _delivery_and_review_prompts()
    assert "_ownerRows" in with_rows["Review wave"]
    assert with_rows["Review wave"].count("_ownerRows") == 1
    assert "_ownerRows" not in with_rows["Review prep"]
    assert "_ownerRows" not in without["Review wave"]
    assert with_rows["Review prep"] == without["Review prep"]


def test_the_mechanical_prep_flag_is_gone_and_the_default_prep_is_the_multi_step_agent():
    with pytest.raises(TypeError):
        _delivery_and_review_prompts(mechanical_prep=True)
    assert "freeze-review-diff" in _delivery_and_review_prompts()["Review prep"]



def _ratio_fragment(*, kira_min=None, verifier=True) -> dict:
    fragment = _v5_fragment()
    agents = fragment["execute_review"]["stages"][1]["agents"]
    agents[0]["executor_rows_per_reviewer"] = {"min": 3, "max": 4}
    if kira_min:
        agents[1]["min_tshirt"] = kira_min
    if not verifier:
        agents.pop(3)
        del fragment["blocking_verdicts"]["coordinator:delivery-verifier"]
    return fragment


def _ratio_review(**kw):
    return parse_execute_review(_ratio_fragment(**kw), signals={"named": ["coordinator:staff-eng"]})


def _grouped_prompts(rows, slice_count):
    """Run the emitted ratio wave under node with agent stubbed; one entry per slice-reviewer prompt."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    slices = [
        {"id": f"s{i}", "files": [f"f{i}.py"], "diff_path": f"d/{i}.diff", "sidecar_path": f"sc{i}.md"}
        for i in range(slice_count)
    ]
    prep = {"verdict": "PARTITION-MANDATORY", "product_files": 9, "slices": slices,
            "whole_diff_path": "d/w.diff", "whole_diff_sidecars": {"personas": ["p.md"]}}
    block = _wave_block(_ratio_review(), executor_rows=rows)
    body = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("phase("))
    script = (
        f"const _reviewPrep = {json.dumps(prep)}; const _verifyOnly = false; const _out = [];\n"
        "const agent = async (p, o) => { _out.push(p); return null; };\n"
        "const parallel = (fns) => Promise.all(fns.map(f => f()));\n"
        f"(async () => {{ {body}\n console.log(JSON.stringify(_out)); }})();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return [p for p in json.loads(out.stdout) if "Slice" in p]


def test_ratio_slice_agent_groups_slices_instead_of_one_reviewer_per_slice():
    block = _wave_block(_ratio_review(), executor_rows=9)
    assert "_groupSlices((Array.isArray(_reviewPrep?.slices) ? _reviewPrep.slices : []), 9, 3, 4).map(g => () =>" in block
    assert "(_reviewPrep?.slices ?? []).map(s =>" not in block
    assert "Math.min(slices.length, Math.ceil(rows / maxRows), Math.floor(rows / minRows))" in block


@pytest.mark.parametrize("rows,slices,expected", [(1, 5, 1), (4, 5, 1), (5, 5, 1), (8, 5, 2), (9, 5, 3), (12, 5, 3), (9, 2, 2)])
def test_grouped_reviewer_count_is_ceil_rows_over_max_floored_by_min_and_capped_at_slices(rows, slices, expected):
    prompts = _grouped_prompts(rows, slices)
    assert len(prompts) == expected
    owned = sum(p.count('"id":"s') for p in prompts)
    assert owned == slices


def test_single_slice_group_prompt_equals_the_per_slice_prompt():
    review = _ratio_review()
    plain = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    one = _grouped_prompts(1, 1)
    assert len(one) == 1 and "Slices: " not in one[0] and "\n\nSlice: " in one[0]
    assert "Slice: ' + JSON.stringify(s)" not in _wave_block(review, executor_rows=1)
    assert "JSON.stringify(s)" in _wave_block(plain)


def test_multi_slice_group_names_every_slice_and_the_sidecar_rule():
    (prompt,) = _grouped_prompts(4, 3)
    assert "\n\nSlices: " in prompt and "first listed slice's id" in prompt and "joined with" not in prompt
    assert prompt.count('"sidecar_path"') == 3


def test_executor_rows_is_required_when_a_slice_agent_carries_a_ratio():
    with pytest.raises(ValueError):
        _wave_block(_ratio_review())
    with pytest.raises(ValueError):
        _wave_block(_ratio_review(), executor_rows=3, executor_rows_js="n")
    assert "(_manifest?.rows ?? []).length" in _wave_block(
        _ratio_review(), executor_rows_js="(_manifest?.rows ?? []).length"
    )


def test_fragment_without_a_ratio_is_unchanged_by_the_new_kwargs():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    assert _wave_block(review, executor_rows=7, tshirt="M") == _wave_block(review)


@pytest.mark.parametrize("size,present", [("XS", False), ("S", False), ("M", False), ("L", True), ("XL", True), ("XXL", True), (None, True)])
def test_min_tshirt_agent_is_composed_only_at_or_above_the_floor(size, present):
    block = _wave_block(_ratio_review(kira_min="L"), executor_rows=4, tshirt=size)
    assert ("coordinator:overengineering-reviewer" in block) is present
    assert "coordinator:code-reviewer" in block


def _verifier_gated_review():
    fragment = _ratio_fragment()
    fragment["execute_review"]["stages"][1]["agents"][3]["min_tshirt"] = "L"
    return parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})


def test_a_compile_time_size_exclusion_names_the_size_in_the_delivery_reason():
    block = _wave_block(_verifier_gated_review(), executor_rows=4, tshirt="M")
    assert "const _deliveryNotRun = 'size M excludes coordinator:delivery-verifier';" in block


def test_an_unknown_compile_time_size_admits_the_delivery_verifier():
    block = _wave_block(_verifier_gated_review(), executor_rows=4)
    assert "excludes coordinator:delivery-verifier" not in block
    assert "const _deliveryVerdict = _reviewWave[_reviewWave.length - 1];" in block


def test_tshirt_js_gates_at_run_time_and_admits_an_unknown_value():
    block = _wave_block(_ratio_review(kira_min="L"), executor_rows=4, tshirt_js="_gate?.tshirt")
    assert '((_gate?.tshirt) == null || ["L", "XL", "XXL"].includes(_gate?.tshirt))' in block
    assert "!" + json.dumps(["XS", "S", "M", "L", "XL", "XXL"]) not in block


def test_tshirt_and_tshirt_js_together_raise():
    with pytest.raises(ValueError):
        _wave_block(_ratio_review(), executor_rows=1, tshirt="L", tshirt_js="t")


def test_roster_without_a_delivery_verifier_binds_the_not_rostered_slot():
    from coordinator_core.ops.review_mint.roster import DELIVERY_NOT_ROSTERED

    block = _wave_block(_ratio_review(verifier=False), executor_rows=4)
    assert "const _deliveryVerdict = null;" in block
    assert f"const _deliveryNotRun = '{DELIVERY_NOT_ROSTERED}';" in block
    assert "delivery-verifier" not in block.replace(DELIVERY_NOT_ROSTERED, "")


def _in_wave(review=None, **kw):
    kw.setdefault("executor_rows", 9)
    return compose_execute_review(
        review or _ratio_review(),
        stage_schemas=_STAGE_SCHEMAS,
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        prep_in_wave=True,
        **{"plan_path": "docs/plans/example.md", **kw},
    )


def test_prep_in_wave_off_is_byte_identical_to_the_default():
    review = _ratio_review()
    kw = dict(
        stage_schemas=_STAGE_SCHEMAS, plan_path="p.md", run_base_sha="a" * 40,
        declared_paths=["x.py"], executor_rows=9,
    )
    assert compose_execute_review(review, prep_in_wave=False, **kw) == compose_execute_review(review, **kw)


def test_prep_in_wave_emits_no_prep_agent_and_no_prep_phase():
    phases = _in_wave()
    assert [t for t, _ in phases] == ["Review wave", "Review integration"]
    text = "\n".join(b for _, b in phases)
    assert "Review prep" not in text and "review:coordinator:test-runner" not in text
    assert "model: 'haiku'" not in text


def test_prep_in_wave_every_wave_agent_runs_review_prep_first_and_returns_prep_beside_review():
    wave = dict(_in_wave())["Review wave"]
    calls = wave.split("agent(")[1:]
    assert len(calls) == 4  # one slice-group fan-out plus three whole-diff agents
    for call in calls:
        assert "review.prep" in call.split(", { label")[0]
        assert "\"required\": [\"prep\"]" in call
        assert "\"review\": {" in call
        assert REVIEW_PREP_BUSY_PREFIX in call
        assert f"up to {_PREP_BUSY_RETRIES} times" in call
    slice_call, *whole_calls = calls
    assert "\"group_index\":<group_index>" in slice_call and "group_index: " in slice_call
    for call in whole_calls:
        assert "group_index" not in call
    assert "_groupSlices" not in wave and "_reviewPrep?." not in wave.split("const _waveRaw")[0]


def test_prep_in_wave_slice_agent_without_a_ratio_raises():
    plain = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    with pytest.raises(ValueError, match="executor_rows_per_reviewer"):
        _in_wave(plain)


def _run_in_wave(prep_full: dict, rows: int, *, landed=None, fail_all=False, ratio_review=None, patch=None) -> dict:
    """Run the prep_in_wave wave block under node with every agent stubbed to return what the
    op would: the whole reply for a whole-diff agent, one group of slices for a slice agent."""
    import shutil
    import subprocess

    from coordinator_core.ops import review_prep as rp

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    wave, *_ = (b for _, b in _in_wave(ratio_review, executor_rows=rows))
    body = "\n".join(ln for ln in wave.splitlines() if not ln.lstrip().startswith("phase("))
    slices = prep_full.get("slices") or []
    script = (
        f"const _full = {json.dumps(prep_full)}; const _failAll = {json.dumps(fail_all)};\n"
        f"const _group = (i, n) => {{ const gs = {json.dumps({str(n): [[s['id'] for s in g] for g in rp.group_slices(slices, n)] for n in range(1, 8)})}[String(n)] ?? []; "
        "const ids = gs[i] ?? []; return { ..._full, slices: _full.slices.filter(s => ids.includes(s.id)) }; };\n"
        "const _spawned = []; let _waveNotRun = null;\n"
        "const agent = async (p, o) => {\n"
        "  if (_failAll) throw new Error('rejected');\n"
        "  const m = /group_index: (\\d+)\\ngroup_count: (\\d+)/.exec(p);\n"
        "  _spawned.push(m ? [Number(m[1]), Number(m[2])] : 'whole');\n"
        "  const _r = { prep: m ? _group(Number(m[1]), Number(m[2])) : _full, review: { v: o.agentType } };\n"
        f"  return ({patch or '(r, n) => r'})(_r, _spawned.length - 1);\n"
        "};\n"
        "const parallel = (fns) => Promise.all(fns.map(f => f()));\n"
        "(async () => { try { const _out = await (async () => {\n"
        + (f"const _landed = {json.dumps(landed)};\n" if landed is not None else "")
        + body
        + "\n return { prep: _reviewPrep, wave: _reviewWave, verifyOnly: _verifyOnly, spawned: _spawned,"
        " delivery: _deliveryVerdict }; })();\n console.log(JSON.stringify({ ..._out, notRun: _waveNotRun }));\n"
        "} catch (e) { console.log(JSON.stringify({error: e.message})); } })();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def test_wave_schema_requires_only_prep_and_the_consumer_enforces_review():
    wave = dict(_in_wave())["Review wave"]
    assert "const _busyPrefix" in wave and "r.review === undefined" in wave
    assert '"required": ["prep"]' in _wave_schema_literal("{}")


def test_a_wave_return_with_no_review_beside_a_clean_prep_is_refused():
    result = _run_in_wave(_FULL, 9, patch="(r, n) => n === 0 ? { prep: r.prep } : r")
    assert result["halted"] == REVIEW_MISSING_HALT


def test_a_rejected_whole_diff_slot_runs_into_a_named_not_run_entry_at_its_wave_index():
    result = _run_in_wave(_FULL, 9, patch="(r, n) => { throw new Error('boom'); }")
    n = len(_run_in_wave(_FULL, 9)["wave"])
    by_slot = {e["slot"]: e for e in result["notRun"] if e["slot"] >= n - 3}
    assert sorted(by_slot) == [n - 3, n - 2]
    assert by_slot[n - 3]["agent_type"] == "coordinator:overengineering-reviewer"
    assert all(e["reason"] == "rejected: boom" for e in by_slot.values())


def test_a_whole_diff_slot_replying_prep_alone_records_returned_no_review():
    base = _run_in_wave(_FULL, 9)
    assert base["notRun"] == []
    first_whole = base["spawned"].index("whole")
    result = _run_in_wave(_FULL, 9, patch=f"(r, n) => (n === {first_whole} ? {{ prep: r.prep }} : r)")
    assert result["notRun"] == [
        {
            "slot": len(base["wave"]) - 3,
            "agent_type": "coordinator:overengineering-reviewer",
            "reason": "returned no review",
        }
    ]


def test_six_groups_over_three_slices_record_three_skipped_no_slices_in_group():
    result = _run_in_wave(
        {**_FULL, "slices": _SLICES[:3]},
        99,
        patch="(r, n) => (r.prep.slices.length ? r : { prep: r.prep })",
    )
    assert "halted" not in result and "error" not in result
    assert [r is None for r in result["wave"][:6]].count(True) == 3
    empty = [e for e in result["notRun"] if e["reason"] == "skipped: no slices in group"]
    assert len(empty) == 3 and len(result["notRun"]) == 3
    assert all(result["wave"][e["slot"]] is None and e["slot"] < 6 for e in empty)
    assert all(e["agent_type"] == "coordinator:code-reviewer" for e in empty)


def test_a_slice_slot_with_slices_and_no_review_records_returned_no_review():
    result = _run_in_wave(_FULL, 9, patch="(r, n) => (n === 0 ? { prep: r.prep, review: null } : r)")
    assert result["halted"] == REVIEW_MISSING_HALT
    assert [e["reason"] for e in result["notRun"]] == ["returned no review"]
    assert result["notRun"][0]["slot"] == 0


def test_a_wave_agent_whose_group_got_no_slices_may_return_prep_alone():
    result = _run_in_wave(_FULL, 9, patch="(r, n) => n === 0 ? { prep: { ...r.prep, slices: [] } } : r")
    assert result.get("halted") != REVIEW_MISSING_HALT


def test_a_wave_return_with_no_review_beside_a_refused_prep_is_accepted():
    result = _run_in_wave(_FULL, 9, patch="(r, n) => n === 0 ? { prep: { ...r.prep, error: 'freeze failed' } } : r")
    assert result.get("halted") != REVIEW_MISSING_HALT


def test_a_busy_prep_in_the_wave_halts_rather_than_merging_a_prep_without_its_slices():
    patch = f"(r, n) => n === 0 ? {{ prep: {{ ...r.prep, slices: [], error: {json.dumps(REVIEW_PREP_BUSY_PREFIX + ' x')} }} }} : r"
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert result["halted"].startswith(PREP_BUSY_HALT)
    assert result["prep"]["error"].startswith(REVIEW_PREP_BUSY_PREFIX)
    assert result["wave"] is None


# Fast tier keeps one malformed-id shape; the rest hit the same membership check and run at cadence.
@pytest.mark.parametrize("bad_id", [
    "s0..s4",
    pytest.param("prep-1..5", marks=pytest.mark.cadence),
    pytest.param("s0+nope", marks=pytest.mark.cadence),
])
def test_a_wave_review_with_a_slice_id_naming_no_real_slice_halts(bad_id):
    patch = (
        f"(r, n) => n === 0 ? {{ prep: {{ ...r.prep, slices: [] }}, "
        f"review: {{ ...r.review, slice_id: {json.dumps(bad_id)} }} }} : r"
    )
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert result["halted"].startswith(SLICE_ID_UNKNOWN_HALT)
    assert json.dumps(bad_id) in result["halted"]
    assert result["wave"] is None and result["integration"] is None


def test_a_bound_wave_review_with_a_truncated_slice_id_is_rebound_from_its_prep_and_does_not_halt():
    patch = "(r, n) => n === 0 ? { ...r, review: { ...r.review, slice_id: 's0+s' } } : r"
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == "s0+s1"


def test_a_wave_review_with_plus_joined_real_slice_ids_is_accepted():
    patch = "(r, n) => n === 0 ? { ...r, review: { ...r.review, slice_id: 's0+s1' } } : r"
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert "halted" not in result and "error" not in result


def _slice_row(slice_id, i):
    return {"id": slice_id, "files": [f"f{i}.py"], "diff_path": f"d/{i}.diff", "sidecar_path": "p.md"}


_RUN_PREP_ID = prep_slice_id_for("docs/plans/p.md", "e964d43cd66e" + "0" * 28, "run-f4068c")
_LONG_SLICES = [_slice_row(f"{_RUN_PREP_ID}-{i + 1}", i) for i in range(3)]
_LONG_PREP = {**_PREP, "verdict": "PARTITION-MANDATORY", "product_files": 9, "slices": _LONG_SLICES, "error": None}


def _with_slice_id(returned):
    """Reply 0 returns an unbound slice_id (its own prep names no slices); the other replies keep
    the group slices their prep honestly named, and the whole-diff replies carry every slice."""
    return (
        f"(r, n) => n === 0 ? {{ prep: {{ ...r.prep, slices: [] }}, review: {{ ...r.review, slice_id: {json.dumps(returned)} }} }}"
        " : r"
    )


def test_a_grouped_wave_review_naming_every_prepped_slice_id_reaches_a_valid_wave():
    ids = [s["id"] for s in _LONG_SLICES]
    assert len(ids) >= 2 and all(i.startswith(_RUN_PREP_ID + "-") for i in ids)
    result = _run_in_wave(_LONG_PREP, 9, patch=_with_slice_id("+".join(ids)))
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == "+".join(ids)
    assert sorted(s["id"] for s in result["prep"]["slices"]) == ids


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param("prep-1+…+prep-3", marks=pytest.mark.cadence),
        pytest.param("...e964d43cd66e-prep-1+...e964d43cd66e-prep-2", marks=pytest.mark.cadence),
        "prep",
        pytest.param("...", marks=pytest.mark.cadence),
        "prep-9",
        pytest.param(f"{_RUN_PREP_ID}-1+nope", marks=pytest.mark.cadence),
    ],
)
def test_a_wave_review_slice_id_that_is_not_an_exact_prepped_id_halts(returned):
    result = _run_in_wave(_LONG_PREP, 9, patch=_with_slice_id(returned))
    assert result["halted"].startswith(SLICE_ID_UNKNOWN_HALT)
    assert result["wave"] is None


_WIDE_SLICES = [_slice_row(f"{_RUN_PREP_ID}-prep-{i + 1}", i) for i in range(3)]
_WIDE_PREP = {**_LONG_PREP, "slices": _WIDE_SLICES}


@pytest.mark.parametrize(
    "returned",
    [
        "prep-1+prep-2",
        f"{_RUN_PREP_ID[:-3]}-prep-1+{_RUN_PREP_ID[:-3]}-prep-2",
        f"{_RUN_PREP_ID[:-8]}-prep-1+{_RUN_PREP_ID[:-8]}-prep-2",
    ],
)
def test_a_truncated_slice_id_matching_exactly_one_real_id_is_attributed_not_halted(returned):
    result = _run_in_wave(_WIDE_PREP, 9, patch=_with_slice_id(returned))
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == f"{_RUN_PREP_ID}-prep-1+{_RUN_PREP_ID}-prep-2"


def test_a_joined_reply_with_a_prefix_truncated_tail_binds_the_unclaimed_real_id():
    returned = f"{_RUN_PREP_ID}-prep-1+{_RUN_PREP_ID}-p"
    two = {**_WIDE_PREP, "slices": _WIDE_SLICES[:2]}
    result = _run_in_wave(two, 9, patch=_with_slice_id(returned))
    assert "halted" not in result and "error" not in result
    assert result["wave"][0]["slice_id"] == f"{_RUN_PREP_ID}-prep-1+{_RUN_PREP_ID}-prep-2"


def test_a_prefix_truncated_token_with_two_unclaimed_candidates_still_halts():
    result = _run_in_wave(_WIDE_PREP, 9, patch=_with_slice_id(f"{_RUN_PREP_ID}-p"))
    assert result["halted"].startswith(SLICE_ID_UNKNOWN_HALT)


@pytest.mark.parametrize("returned", ["prep-", "prep-9", f"{_RUN_PREP_ID[:-8]}"])
def test_an_ambiguous_or_unmatched_slice_id_still_halts(returned):
    result = _run_in_wave(_WIDE_PREP, 9, patch=_with_slice_id(returned))
    assert result["halted"].startswith(SLICE_ID_UNKNOWN_HALT)


def test_a_wave_brief_tells_agents_to_return_the_exact_listed_ids():
    prompt = "\n".join(b for _, b in _in_wave())
    assert "copied verbatim" in prompt and "short alias" not in prompt and "prep-1+prep-2" not in prompt


def test_wave_schema_types_prep_slices_as_an_array():
    assert '"slices": { "type": "array"' in _wave_schema_literal("{}")


_SLICES = [
    {"id": f"s{i}", "files": [f"f{i}.py"], "diff_path": f"d/{i}.diff", "sidecar_path": "p.md"} for i in range(5)
]
_FULL = {**_PREP, "verdict": "PARTITION-MANDATORY", "product_files": 9, "slices": _SLICES, "error": None}


@pytest.mark.parametrize("rows,groups", [
    (1, 1),
    pytest.param(4, 1, marks=pytest.mark.cadence),
    (8, 2),
    pytest.param(9, 3, marks=pytest.mark.cadence),
    pytest.param(12, 3, marks=pytest.mark.cadence),
    (99, 6),
])
def test_prep_in_wave_fans_out_the_ratio_reviewer_count_and_merges_every_group_back(rows, groups):
    result = _run_in_wave(_FULL, rows)
    assert "halted" not in result and "error" not in result
    group_calls = [c for c in result["spawned"] if c != "whole"]
    assert len(group_calls) == groups and {n for _, n in group_calls} == {groups}
    assert result["spawned"].count("whole") == 3
    assert [s["id"] for s in result["prep"]["slices"]] == [s["id"] for s in _SLICES]
    assert len(result["wave"]) == groups + 3
    assert result["delivery"] == result["wave"][-1] and result["delivery"]["v"] == "general-purpose"


def test_prep_in_wave_binds_reviewprep_from_the_first_reply_without_an_error():
    result = _run_in_wave({**_FULL, "slices": [_SLICES[0]]}, 9)
    assert result["prep"]["verdict"] == "PARTITION-MANDATORY"
    assert [s["id"] for s in result["prep"]["slices"]] == ["s0"]


def test_prep_in_wave_single_reviewer_ok_over_product_files_synthesises_one_whole_diff_slice():
    result = _run_in_wave({**_PREP, "slices": [], "error": None}, 4)
    assert [s["id"] for s in result["prep"]["slices"]] == ["whole-diff"]
    assert result["prep"]["slices"][0]["sidecar_path"] == "p.md"


def test_prep_in_wave_no_product_file_is_a_no_op_after_the_wave():
    result = _run_in_wave({**_PREP, "product_files": 0, "slices": [], "error": None}, 4)
    assert result["halted"] == "no-op" and result["prep"]["product_files"] == 0
    assert result["wave"] is None and result["integration"] is None


def test_prep_in_wave_landed_rows_with_no_product_file_proceed_verify_only():
    result = _run_in_wave({**_PREP, "product_files": 0, "slices": [], "error": None}, 4, landed=_LANDED)
    assert "halted" not in result and result["verifyOnly"] is True and result["prep"]["slices"] == []


@pytest.mark.parametrize(
    "prep",
    [
        {**_PREP, "verdict": "PARTITION-MANDATORY", "product_files": 0, "slices": [], "error": None},
        {**_PREP, "product_files": 0, "foreign_claims": ["peer.py"], "slices": [], "error": None},
        {**_PREP, "slices": [], "verdict": None, "error": "freeze failed"},
    ],
)
def test_prep_in_wave_an_empty_or_refused_prep_halts_with_todays_text(prep):
    result = _run_in_wave(prep, 4, landed=_LANDED)
    assert result["halted"].startswith("review prep returned no slices (prep verdict: ")
    assert result["wave"] is None and result["integration"] is None
    assert result["prep"]["error"] == prep["error"]


def test_prep_in_wave_every_agent_rejecting_halts_as_a_failed_prep():
    result = _run_in_wave(_FULL, 4, fail_all=True)
    assert result["halted"] == "review prep returned no slices (prep verdict: none)"
    assert result["prep"] is None


def test_wave_schema_declares_prep_slices_array_of_objects_and_error_string_or_null():
    schema = json.loads(_wave_schema_literal("{}"))
    prep = schema["properties"]["prep"]
    assert prep["properties"]["slices"] == {"type": "array", "items": {"type": "object"}}
    assert prep["properties"]["error"] == {"type": ["string", "null"]}
    assert schema["required"] == ["prep"]


def test_no_composed_slices_iteration_is_unguarded():
    import re

    blocks = [b for _, b in _in_wave()] + [b for _, b in _in_wave(executor_rows=1)]
    script = "\n".join(blocks)
    assert "slices" in script
    assert not re.findall(r"\w+(?:\?)?\.slices \?\? \[\]", script)
    assert not re.findall(r"\.slices\.length", script)


@pytest.mark.parametrize("bad", [3, pytest.param("3", marks=pytest.mark.cadence), None, pytest.param({"n": 3}, marks=pytest.mark.cadence)])
def test_a_summarised_prep_reply_halts_instead_of_throwing(bad):
    patch = f"(r, n) => n === 0 ? {{ ...r, prep: {{ ...r.prep, slices: {json.dumps(bad)} }} }} : r"
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert "error" not in result
    assert result["halted"].startswith(PREP_MALFORMED_HALT)
    assert result["wave"] is None


def test_empty_relayed_slices_do_not_halt_when_a_wave_review_names_slices():
    patch = (
        "(r, n) => n === 0 ? { prep: { ...r.prep, slices: [] }, "
        "review: { v: 'x', slice_id: 'm-prep-1+m-prep-2', sidecar_path: 'p1.md' } } : { ...r, prep: { ...r.prep, slices: [] } }"
    )
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert "error" not in result and "halted" not in result
    assert [s["id"] for s in result["prep"]["slices"]] == ["m-prep-1", "m-prep-2"]


def test_empty_relayed_slices_with_no_review_slice_id_still_halt():
    patch = "(r, n) => ({ ...r, prep: { ...r.prep, slices: [] } })"
    result = _run_in_wave(_FULL, 9, patch=patch)
    assert result["halted"].startswith("review prep returned no slices")


def _reverify_phases(**kw):
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    return compose_execute_review(
        review,
        stage_schemas=_STAGE_SCHEMAS,
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        declared_paths=["x.py"],
        **kw,
    )


def test_reverify_leg_requires_and_asks_for_failing_detail():
    text = "\n".join(t + body for t, body in _reverify_phases(reverify=True) if t == "Review re-verify")
    assert "failing_detail" in text
    assert "one-line assertion message" in text
    assert text.count("failing [], failing_detail []") >= 2
    assert "failing [], reverted []" not in text


def test_reverify_false_output_equals_the_default_output():
    assert _reverify_phases(reverify=False) == _reverify_phases()


def test_reverify_true_places_the_phase_after_integration():
    titles = [t for t, _ in _reverify_phases(reverify=True)]
    assert titles == ["Review prep", "Review wave", "Review integration", "Review re-verify"]


def test_reverify_without_integration_follows_the_wave():
    fragment = _v5_fragment()
    fragment["execute_review"]["stages"].pop()
    review = parse_execute_review(fragment, signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review, stage_schemas=_STAGE_SCHEMAS, plan_path="docs/plans/example.md",
        run_base_sha="a" * 40, declared_paths=["x.py"], reverify=True,
    )
    assert [t for t, _ in phases] == ["Review prep", "Review wave", "Review re-verify"]


def test_reverify_prompt_names_the_op_the_agent_and_forbids_a_suite_run():
    block = dict(_reverify_phases(reverify=True))["Review re-verify"]
    assert "const _reviewReverify = _verifyOnly ? null :" in block
    assert "agentType: 'coordinator:test-runner'" in block
    assert block.count("review.reverify") >= 2
    assert "never a directory, a tier (the fast tier included) or the full suite" in block
    assert "never `git checkout`" in block
    assert "every file in `tripwires`" in block
    assert f'\\"slice_prefix\\":\\"{prep_slice_id_for("docs/plans/example.md", "a" * 40)}\\"' in block or (
        f'"slice_prefix":"{prep_slice_id_for("docs/plans/example.md", "a" * 40)}"' in block
    )
    assert "catch (e) { return null; }" in block
    assert "review_sidecars" in block


def test_reverify_prompt_probes_pre_review_status_before_revert():
    block = dict(_reverify_phases(reverify=True))["Review re-verify"]
    assert block.count("review.reverify") >= 4
    assert "failing_before" in block
    assert block.count('probe\\":\\"open') + block.count('probe":"open') >= 1
    close_at = max(block.find('probe\\":\\"close'), block.find('probe":"close'))
    revert_at = max(block.find('failing_before\\":['), block.find('failing_before":['))
    assert 0 <= close_at < revert_at
    assert '"pre_existing"' in block and '"kept"' in block


def test_reverify_degrades_to_general_purpose_on_a_degraded_host():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    phases = compose_execute_review(
        review, stage_schemas=_STAGE_SCHEMAS, plan_path="docs/plans/example.md",
        run_base_sha="a" * 40, declared_paths=["x.py"], reverify=True, host_degraded=True,
    )
    block = dict(phases)["Review re-verify"]
    assert "coordinator:" not in block
    assert "agentType: 'general-purpose'" in block


def test_reverify_slice_key_sentinel_is_resolved():
    block = dict(_reverify_phases(reverify=True, slice_key_js="_k"))["Review re-verify"]
    assert "__SLICE_KEY__" not in block
    assert "String(_k)" in block


def test_a_run_key_reaches_the_prep_and_probe_open_calls_so_the_foreign_path_filters_fire():
    key = "warp-x-20261025T000000-aaaaaa"
    # In-wave prep runs in run mode (its one caller, ask_compose); review.prep refuses run_id beside paths.
    wave = dict(_in_wave(run_key=key, prep_run_id=key, plan_path="state/warp/r/manifest.json"))["Review wave"].replace('\\"', '"')
    reverify = dict(_reverify_phases(reverify=True, run_key=key))["Review re-verify"].replace('\\"', '"')
    assert f'"worktree":true,"run_id":"{key}","base"' in wave
    assert f'"run_id":"{key}","plan_path":"docs/plans/example.md","probe":"open"' in reverify
    assert "run_id" not in dict(_in_wave())["Review wave"]
    assert "run_id" not in dict(_reverify_phases(reverify=True))["Review re-verify"]


def _reverify_phase_block(base_ref, group_prefixes=()):
    from coordinator_core.ops.review_mint.execute_review import _reverify_phase

    return _reverify_phase(
        base_ref=base_ref, prep_slice_id="prep1", repo_root=None, plan_path="docs/plans/example.md",
        run_base_sha=base_ref, prompt_head="head", host_degraded=False,
        sc=lambda call, *_: call, group_prefixes=group_prefixes,
    )[1]


@pytest.mark.parametrize("prefixes", [(), ("g1", "g2")])
def test_reverify_prompt_measures_failing_at_base_once_and_passes_it(prefixes):
    block = _reverify_phase_block("b" * 40, prefixes)
    assert block.count("baseline_export") == 1
    assert "--sha " + "b" * 40 in block
    assert "pytest -rfE" in block
    assert block.count("failing_at_base") >= 3
    assert block.count('failing_at_base\\":[<failing_at_base ids>]') + block.count(
        'failing_at_base":[<failing_at_base ids>]'
    ) == 1


@pytest.mark.parametrize("prefixes", [(), ("g1", "g2")])
def test_reverify_prompt_omits_the_base_run_when_the_base_is_unset(prefixes):
    block = _reverify_phase_block("run_base_sha", prefixes)
    assert "baseline_export" not in block
    assert "failing_at_base" in block


@pytest.mark.parametrize("prefixes", [(), ("g1", "g2")])
def test_reverify_reports_passed_alone_and_passed_at_base(prefixes):
    from coordinator_core.ops.review_mint.execute_review import _REVERIFY_SCHEMA

    for field in ("passed_alone", "passed_at_base"):
        assert _REVERIFY_SCHEMA["properties"][field] == {"type": "array", "items": {"type": "string"}}
        assert field not in _REVERIFY_SCHEMA["required"]
    measured = _reverify_phase_block("b" * 40, prefixes)
    assert "ALONE (one pytest per id)" in measured and "`passed_alone`" in measured
    assert "absence at base is not a pass" in measured
    assert '"ok"' in measured and "`runner_exit` 0 or 1" in measured
    unset = _reverify_phase_block("run_base_sha", prefixes)
    assert "ALONE (one pytest per id)" in unset
    assert "Return `passed_at_base` []" in unset
    assert "absence at base" not in unset


def _run_reverify_block(reply, *, verify_only=False, applied=1, deferred=False, calls=None):
    """Run the emitted re-verify block under node with the agent stubbed to return ``reply``.

    ``applied`` is the first wave review's applied count; ``calls`` (a list) receives the number of agent calls made."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    block = dict(_reverify_phases(reverify=True, reverify_deferred=deferred))["Review re-verify"]
    body = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("phase("))
    var = "_reviewReverifyP" if deferred else "_reviewReverify"
    prep = {"slices": [
        {"id": "s1", "files": ["a/x.py"], "sidecar_path": "prep-s1.md"},
        {"id": "s2", "files": ["b/y.py"], "sidecar_path": "prep-s2.md"},
    ]}
    wave = [{"slice_id": "s1", "sidecar_path": "wave-s1.md", "applied": applied}, None]
    script = (
        f"const _reviewPrep = {json.dumps(prep)}; const _reviewWave = {json.dumps(wave)};\n"
        f"const _verifyOnly = {json.dumps(verify_only)}; const _reviewIntegration = {{ fixes_applied: 0 }};\n"
        f"let _n = 0; const agent = async (p, o) => {{ _n++; return {json.dumps(reply)}; }};\n"
        f"(async () => {{ {body}\n console.log(JSON.stringify([await {var}, _n])); }})();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    result, count = json.loads(out.stdout)
    if calls is not None:
        calls.append(count)
    return result


def test_unapplied_maps_a_reverted_path_to_its_slice_and_wave_sidecar():
    reply = {
        "edited": ["a/x.py", "b/y.py", "new.py"], "failing": ["a/test_x.py::t"],
        "reverted": [
            {"path": "a\\x.py", "tests": ["a/test_x.py::t"],
             "findings": [{"sidecar": "wave-s1.md", "id": "F1"}, {"sidecar": "other.md", "id": "F9"}]},
            {"path": "b/y.py", "tests": [], "findings": [{"sidecar": "prep-s2.md", "id": "F2"}]},
            {"path": "new.py", "tests": [], "findings": []},
        ],
        "status_before": "fail", "status_after": "pass",
    }
    result = _run_reverify_block(reply)
    assert result["status_after"] == "pass"
    assert result["unapplied"] == [
        {"path": "a\\x.py", "slice_id": "s1", "sidecar_path": "wave-s1.md", "finding_ids": ["F1"]},
        {"path": "b/y.py", "slice_id": "s2", "sidecar_path": "prep-s2.md", "finding_ids": ["F2"]},
        {"path": "new.py", "slice_id": None, "sidecar_path": None, "finding_ids": []},
    ]


def test_reverify_is_null_under_verify_only_and_when_the_agent_returns_null():
    assert _run_reverify_block({"edited": []}, verify_only=True) is None
    assert _run_reverify_block(None) is None


def test_reverify_runs_as_pinned_haiku_not_a_sonnet_override():
    block = dict(_reverify_phases(reverify=True))["Review re-verify"]
    assert "agentType: 'coordinator:test-runner', model: 'haiku'" in block
    assert "model: 'sonnet'" not in block
    degraded = dict(_reverify_phases(reverify=True, host_degraded=True))["Review re-verify"]
    assert "model:" not in degraded


def test_reverify_runs_when_the_review_applied_no_edit():
    # Halted-run self-correction reads owned and foreign reds off a wave that applied nothing.
    calls = []
    assert _run_reverify_block({"edited": ["a/x.py"]}, applied=0, calls=calls) is not None
    assert calls == [1]


def test_deferred_reverify_binds_an_unawaited_promise_that_runs_like_the_awaited_one():
    block = dict(_reverify_phases(reverify=True, reverify_deferred=True))["Review re-verify"]
    assert "const _reviewReverifyP =" in block
    assert "const _reviewReverify =" not in block
    calls = []
    assert _run_reverify_block({"edited": []}, deferred=True, applied=0, calls=calls) is not None
    assert calls == [1]
    assert _run_reverify_block({"edited": []}, deferred=True, verify_only=True) is None


_NOTES_LABEL = "Executor delivery notes in the brief"


def test_delivery_notes_source_unset_leaves_the_wave_byte_identical():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    assert _NOTES_LABEL not in _wave_block(review)
    assert _wave_block(review, delivery_notes_js=None) == _wave_block(review)


def test_delivery_notes_ride_only_the_code_reviewer_slice_prompt():
    review = parse_execute_review(_v5_fragment(), signals={"named": ["coordinator:staff-eng"]})
    block = _wave_block(review, delivery_notes_js="_deliveryNotes")
    assert block.count(_NOTES_LABEL) == 1
    assert "((_deliveryNotes) ? " in block
    assert block.count("agent(") > 1
    notes_call = block.split(_NOTES_LABEL)[1].split("agent(", 1)[0]
    assert "code-reviewer" in notes_call or "code-reviewer" in block.split(_NOTES_LABEL)[0].rsplit("agent(", 1)[-1]


def test_delivery_notes_also_ride_the_grouped_prep_in_wave_prompt():
    block = _wave_block(_ratio_review(), executor_rows_js="n", delivery_notes_js="_deliveryNotes")
    assert block.count(_NOTES_LABEL) == 1
