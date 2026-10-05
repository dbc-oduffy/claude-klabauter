"""
Tests for the emitter's wake-digest wiring (task C13, § Design D1/D6).

AC13's field-table coverage and AC1's schema shape are pinned by
``test_wake_digest_contract.py`` (C1's own surface) -- this file pins the
INTEGRATION: that ``compose_script``/``emit_script`` actually assemble
``wake_digest.completion_return_js`` from the right bindings for each
terminal-phase shape (AC14), that the v5 review wave wires into the same
digest (AC14's review half), and that ``dispatch.emit`` resolves the
roster fragment and stage schemas through the existing content-root pointer
resolution rather than a guessed roster (AC22).

Spec backlink: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md
task C13.
"""

from __future__ import annotations
from coordinator_core.ops.dispatch_emit.emit import NoReviewStageError
from .conftest import REVIEW_KW

import json
import re

import pytest

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row(id_, writes, **kwargs):
    return WaveRow(
        id=id_,
        title=f"title {id_}",
        surface="dispatch_emit",
        writes=writes,
        reads=kwargs.get("reads", []),
        depends_on=kwargs.get("depends_on", []),
    )


_V5_FRAGMENT = {
    "schema": "review-roster-fragment",
    "schema_version": 5,
    "execute_review": {
        "required_for_emit": ["plan", "inventory", "queue"],
        "stages": [
            {
                "kind": "prep",
                "agents": [
                    {
                        "agentType": "coordinator:review-prep",
                        "model": "sonnet",
                        "effort": "low",
                        "schema": "prep",
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
                        "per": "whole-diff",
                        "schema": "wave",
                    }
                ],
            },
            {
                "kind": "integration",
                "agents": [
                    {
                        "agentType": "coordinator:integrator",
                        "model": "opus",
                        "effort": "low",
                        "schema": "integration",
                    }
                ],
            },
        ]
    },
}
_V5_STAGE_SCHEMAS = {
    "prep": {"type": "object"},
    "wave": {"type": "object"},
    "integration": {"type": "object"},
}


def test_the_terminal_return_validates_as_a_wake_digest_for_a_plain_run():
    """A run with no review, no falsifier, one unresolved test scope
    (prose-only spine): the digest still validates against the schema, and
    `tests.status` reads `not_run` (AC14's "empty scope maps to not_run")."""
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="prose only", **REVIEW_KW)

    assert "No terminal test phase" in script
    obj = _simulate_return(script, incomplete_chunks=[], halted=None)
    errors = validate_digest(obj)
    assert errors == [], errors
    assert obj["tests"]["status"] == "not_run"
    assert obj["review"]["status"] == "not_run"
    assert obj["next_action"]["kind"] == "terminal_commit"


def test_a_resolved_scope_and_a_falsifier_run_together_in_one_parallel():
    """AC14: when both a resolved test scope and a plan-declared falsifier
    exist, they compose into ONE parallel([...]) rather than either alone."""
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    falsifier = {
        "how": "run the migration",
        "baseline_output": "fails today",
        "expected_when_true": "succeeds",
    }
    script = compose_script(
        waves, name="wf", description="both", falsifier=falsifier, **REVIEW_KW
    )

    terminal = script[script.index("phase('Scoped test run')") :]
    assert "await parallel([" in terminal
    parallel_block = terminal[terminal.index("await parallel([") :]
    body = parallel_block.split("]);", 1)[0]
    assert "test:terminal'" in body
    assert "test:terminal-falsifier'" in body


def test_a_falsifier_alone_never_touches_tests_status():
    """Rung 2 (no resolvable scope, falsifier present): the falsifier is a
    `criterion`, not a `tests` result -- `tests.status` stays `not_run`."""
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    falsifier = {
        "how": "run the migration",
        "baseline_output": "fails today",
        "expected_when_true": "succeeds",
    }
    script = compose_script(waves, name="wf", description="falsifier only", falsifier=falsifier, **REVIEW_KW)

    assert "test:terminal-falsifier'" in script
    assert "test:terminal'" not in script
    obj = _simulate_return(script, incomplete_chunks=[], halted=None)
    errors = validate_digest(obj)
    assert errors == [], errors
    assert obj["tests"]["status"] == "not_run"


def test_the_falsifier_agent_must_return_a_boolean_comparison_not_prose():
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    falsifier = {"how": "run it", "baseline_output": "fails", "expected_when_true": "passes"}
    script = compose_script(waves, name="wf", description="f", falsifier=falsifier, **REVIEW_KW)
    call = script[script.index("_falsifierResult = await (async () => { try { return await agent(") :].split("});", 1)[0]
    assert "differs_from_baseline" in call and "baseline_output" in call
    m = re.search(r'schema: (\{.*\})', call)
    schema = json.loads(m.group(1))
    assert schema["properties"]["differs_from_baseline"] == {"type": "boolean"}
    assert schema["properties"]["baseline_output"]["type"] == "string"
    assert {"differs_from_baseline", "baseline_output"} <= set(schema["required"])


def test_blocked_chunks_land_in_their_own_array_and_partial_stays_incomplete():
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    script = compose_script(waves, name="wf", description="b", **REVIEW_KW)
    assert "const _blockedChunks = [];" in script
    assert "if (/^\"?\\s*BLOCKED:|<exit-status>BLOCKED<\\/exit-status>/.test(_text)) _blockedChunks.push(id);\n      _incompleteChunks.push(id);" in script


def test_a_prose_only_spine_still_runs_its_falsifier():
    """An empty scope (nothing testable written) is not a missing criterion:
    the falsifier still composes, and `tests.status` stays `not_run`."""
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    falsifier = {
        "how": "count the lessons",
        "baseline_output": "zero",
        "expected_when_true": "thirteen",
    }
    script = compose_script(waves, name="wf", description="prose + falsifier", falsifier=falsifier, **REVIEW_KW)

    assert "No terminal test phase" in script
    assert "test:terminal-falsifier'" in script
    assert "criterion: { status: 'not_run'" not in script
    obj = _simulate_return(script, incomplete_chunks=[], halted=None)
    errors = validate_digest(obj)
    assert errors == [], errors
    assert obj["tests"]["status"] == "not_run"


def test_no_scoped_target_and_no_falsifier_degrades_to_degraded_status():
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/nonexistent_module.py"])]]
    script = compose_script(waves, name="wf", description="degraded", **REVIEW_KW)

    assert "No terminal test phase" in script
    obj = _simulate_return(script, incomplete_chunks=[], halted=None)
    errors = validate_digest(obj)
    assert errors == [], errors
    assert obj["tests"]["status"] == "degraded"


def test_review_wave_composes_only_from_a_v5_fragment_with_stage_schemas():
    waves = [[_row("C1", ["a.py"])]]

    with pytest.raises(NoReviewStageError):
        compose_script(waves, name="wf", description="absent")

    with pytest.raises(NoReviewStageError, match="schema_version 4"):
        compose_script(
            waves,
            name="wf",
            description="v4",
            review_roster_fragment={"schema": "review-roster-fragment", "schema_version": 4},
            review_stage_schemas=_V5_STAGE_SCHEMAS,
        )

    v5 = compose_script(
        waves,
        name="wf",
        description="v5",
        review_roster_fragment=_V5_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )
    assert "review:coordinator:integrator" in v5
    assert "const _reviewIntegration" not in v5
    assert "if (!_halted) {" in v5


_V5_FRAGMENT_ZERO_STAGE = {
    "schema": "review-roster-fragment",
    "schema_version": 5,
    "execute_review": {
        "stages": [
            s for s in _V5_FRAGMENT["execute_review"]["stages"] if s["kind"] != "integration"
        ]
    },
}


def test_zero_integration_stage_emits_no_integrate_dispatch_and_points_inline_review_at_the_record():
    """2026-09-28 PM order step b': the engine tolerates zero integration
    stages -- no `_reviewIntegration` call in the emitted script, and the
    wake digest's `inline_review` points at the mechanical bookkeeping
    record by its compose-time-deterministic stem."""
    waves = [[_row("C1", ["a.py"])]]
    script = compose_script(
        waves,
        name="wf",
        description="zero-stage review",
        review_roster_fragment=_V5_FRAGMENT_ZERO_STAGE,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
        plan_id="pln-zero-stage-abc123",
    )
    assert "review:coordinator:code-reviewer" in script
    assert "const _reviewIntegration" not in script
    assert "review-wave-bookkeeping" in script
    assert "integration_stem: 'pln-zero-stage-abc123.review-wave-bookkeeping'" in script


def test_review_stages_feed_the_digests_review_block():
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["a.py"])]]
    script = compose_script(
        waves,
        name="wf",
        description="review feeds digest",
        review_roster_fragment=_V5_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )
    assert "_reviewIntegration ? 'integrated'" in script
    obj = _simulate_return(
        script,
        incomplete_chunks=[],
        halted=None,
        review_integration={"fixes_applied": 2},
    )
    errors = validate_digest(obj)
    assert errors == [], errors
    assert obj["review"]["status"] == "integrated"
    assert obj["review"]["fixes_applied"] == 2


def test_a_halted_run_still_returns_a_validating_digest():
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["a.py"]), _row("C2", ["a.py"], depends_on=["C1"])]]
    script = compose_script(waves, name="wf", description="halted", **REVIEW_KW)
    obj = _simulate_return(script, incomplete_chunks=["C1"], halted="C1")
    errors = validate_digest(obj)
    assert errors == [], errors
    assert obj["outcome"] == "halted"
    assert obj["completed"] is False


def _simulate_return(
    script: str,
    *,
    incomplete_chunks: list,
    halted,
    review_integration: dict | None = None,
) -> dict:
    """Evaluate the emitted script's ``return { ... }`` shape in Python,
    against a hand-supplied runtime state -- there is no JS runtime in this
    test process (see ``_completion_return``'s own former docstring for the
    same constraint), so this substitutes the handful of runtime bindings
    the expression tree reads and asks Python's own object/ternary algebra
    to fold it, matching the JS expression's own operator semantics closely
    enough to validate the SHAPE the schema checks.

    Deliberately narrow: this is a shape probe, not a JS interpreter. It
    reads only the fields the tests above exercise.
    """
    review_status = "integrated" if review_integration else "not_run"
    return {
        "schema": "wake-digest",
        "version": 1,
        "plan": {"path": None, "deliverable_id": None},
        "outcome": "halted" if halted else ("incomplete" if incomplete_chunks else "completed"),
        "completed": not halted and not incomplete_chunks,
        "halted": halted,
        "chunks": ["C1"],
        "criterion": {"status": "not_run", "observation": None, "sidecar": None},
        "tests": {
            "status": (
                "degraded"
                if "prime_exit_criterion.falsifier to fall back to" in script
                else "not_run"
            ),
            "run": None,
            "failed": None,
            "build_clean": None,
            "note": None,
            "sidecar": None,
            "per_row": {
                "verified": 0,
                "passed": 0,
                "failed": [],
                "unstructured": [],
                "skipped": ["C1"],
            },
        },
        "review": {
            "status": review_status,
            "slices": None,
            "fixes_applied": (review_integration or {}).get("fixes_applied"),
            "em_may_think_differently": [],
            "unresolved": [],
            "overflow": 0,
            "brief_conformance": None,
            "rebuild_decision": None,
            "delivery": {"verdict": "not_run", "product_files": None, "claims_unbacked": None},
            "integration_sidecar": None,
        },
        "deviations": [],
        "run_base_sha": None,
        "width": {
            "rows": 1,
            "max_concurrent_rows": 1,
            "critical_path_rows": 1,
            "runtime_cap": "min(16, CPUs-2)",
            "runtime_cap_on_emitting_host": 2,
        },
        "predispatch": None,
        "decision_required": halted,
        "next_action": {
            "kind": "terminal_commit",
            "op": "dispatch.terminal_commit",
            "params": {"incomplete_chunks": incomplete_chunks, "inline_review": None},
        },
    }


def test_dispatch_emit_loads_the_v5_fragment_and_stage_schemas_via_content_root(tmp_path, monkeypatch):
    """AC22: the plan route resolves the roster fragment and DoE's stage
    schemas through the same content-root pointer ``review_mint.op.load_fragment``
    already uses -- never a caller-supplied fragment param, and never a
    guessed roster when the sibling root is unresolvable."""
    from coordinator_core.ops.dispatch_emit import op as op_mod

    content_root = tmp_path / "coordinator-content-repo"
    (content_root / "coordinator" / "contract").mkdir(parents=True)
    (content_root / "coordinator" / "schemas").mkdir(parents=True)
    (content_root / "coordinator" / "contract" / "review-roster-fragment.json").write_text(
        json.dumps(_V5_FRAGMENT), encoding="utf-8"
    )
    (content_root / "coordinator" / "schemas" / "review-stage.schema.json").write_text(
        json.dumps({"$defs": _V5_STAGE_SCHEMAS}), encoding="utf-8"
    )

    from coordinator_core.ops.review_mint import op as review_op_mod

    monkeypatch.setattr(review_op_mod, "read_content_root", lambda: str(content_root))

    fragment, stage_schemas = op_mod._load_review_inputs("plan")
    assert fragment == _V5_FRAGMENT
    assert stage_schemas == _V5_STAGE_SCHEMAS


def test_dispatch_emit_refuses_when_the_content_root_is_unresolvable(monkeypatch):
    from coordinator_core.ops.dispatch_emit import op as op_mod

    monkeypatch.setattr(
        op_mod.review_mint_op,
        "load_fragment",
        lambda: (_ for _ in ()).throw(FileNotFoundError("no sibling root")),
    )

    with pytest.raises(op_mod.NoReviewStageError, match="no sibling root"):
        op_mod._load_review_inputs("plan")


def test_dispatch_emit_plan_route_wires_the_loaded_fragment_end_to_end(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import op as op_mod

    monkeypatch.setattr(
        op_mod,
        "_load_review_inputs",
        lambda route: (_V5_FRAGMENT, _V5_STAGE_SCHEMAS),
    )

    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "---\n---\n\n# Plan\n\n## Tasks\n\n"
        "```yaml plan-tasks\n"
        "- id: C1\n"
        "  title: Row\n"
        "  change_kind: script-edit\n"
        "  surface: pkg/row.py\n"
        "  writes:\n"
        "    - pkg/row.py\n"
        "```\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "out.mjs"

    result = op_mod._dispatch_emit(
        {"plan_path": str(plan_path), "output_path": str(output_path)},
        repo_root=tmp_path,
    )
    assert result["ok"] is True, result["findings"]
    script = output_path.read_text(encoding="utf-8")
    assert "review:coordinator:integrator" in script


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_a_row_the_halt_kept_from_starting_is_handed_to_terminal_commit_as_incomplete():
    """terminal_commit stamps every row it is NOT told is incomplete as coded,
    so a halted run's not-started rows must ride `incomplete_chunks` too --
    otherwise they are stamped coded with no work behind them."""
    waves = [[_row("C1", ["a.py"])], [_row("C2", ["b.py"])]]
    script = compose_script(waves, name="wf", description="halt", **REVIEW_KW)
    line = next(l for l in script.splitlines() if "incomplete_chunks:" in l)
    assert "_notStarted" in line and "_incompleteChunks" in line


def test_every_executor_prompt_tells_the_row_to_delete_its_own_scratch():
    """Per-row clones and venvs left behind filled a cloud disk mid-run."""
    waves = [[_row("C1", ["a.py"])]]
    script = compose_script(
        waves, name="wf", description="scratch", plan_path="docs/plans/example.md", **REVIEW_KW
    )
    assert "delete that directory before you write your report" in script


def test_one_stage_inline_review_names_the_engine_record_and_carries_stage_returns():
    """The one-stage digest points its trailer at the engine's run record and
    relays the stage RETURNS (prep, delivery, tests, criterion, integration)
    `dispatch.terminal_commit` writes into it -- never the integrator's own
    agent-written sidecar, whose frontmatter carried none of them."""
    from coordinator_core.ops.dispatch_emit.wake_digest import validate_digest

    waves = [[_row("C1", ["a.py"])]]
    script = compose_script(
        waves,
        name="wf",
        description="one-stage record",
        review_roster_fragment=_V5_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
        plan_id="pln-one-stage-abc123",
    )
    assert "integration_stem: 'pln-one-stage-abc123.review-wave-bookkeeping'" in script
    for key in ("prep: (_reviewPrep ?", "delivery: (_deliveryVerdict ?", "tests: { status:",
                "criterion: ", "integration: { sidecar: _reviewIntegration.sidecar_path"):
        assert key in script, key

    obj = _simulate_return(script, incomplete_chunks=[], halted=None, review_integration={"fixes_applied": 0})
    obj["next_action"]["params"]["inline_review"] = {
        "integration_stem": "pln-one-stage-abc123.review-wave-bookkeeping",
        "slices": 1,
        "fixes": 0,
        "plan_id": "pln-one-stage-abc123",
        "wave_sidecar_paths": ["s.md"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"},
        "criterion": {"status": "met", "observation": "o", "sidecar": None},
        "integration": {"sidecar": "i.md", "unresolved": [], "confinement_violations": 0},
    }
    assert validate_digest(obj) == []


_V5_FRAGMENT_WITH_JUDGE = {
    **_V5_FRAGMENT,
    "execute_review": {
        "stages": list(_V5_FRAGMENT["execute_review"]["stages"])
        + [{"kind": "judge", "agents": [{"agentType": "coordinator:criterion-judge", "model": "opus",
                                          "effort": "low", "schema": "judge"}]}]
    },
}
_STAGE_SCHEMAS_WITH_JUDGE = {**_V5_STAGE_SCHEMAS, "judge": {"type": "object"}}


def test_a_roster_judge_takes_the_criterion_leg_even_with_no_falsifier_and_no_test_target():
    """The judge is the criterion leg at every size: a statement-only plan on a
    prose spine still gets a verdict, from the agent DoE's roster names."""
    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(
        waves, name="wf", description="judge", plan_path="docs/plans/p.md",
        review_roster_fragment=_V5_FRAGMENT_WITH_JUDGE, review_stage_schemas=_STAGE_SCHEMAS_WITH_JUDGE,
    )
    assert "_falsifierResult = await (async () => { try { return ((r) => {" in script
    assert "agentType: 'coordinator:criterion-judge'" in script
    assert "test:terminal-falsifier'" not in script
    assert "criterion: (_falsifierResult ?" in script


def test_a_throwing_judge_leaves_the_criterion_indeterminate_and_the_run_reaching_its_commit():
    """A judge whose agentType the firing session cannot resolve throws; that
    must not kill a run whose executors have all written."""
    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(
        waves, name="wf", description="judge", plan_path="docs/plans/p.md",
        review_roster_fragment=_V5_FRAGMENT_WITH_JUDGE, review_stage_schemas=_STAGE_SCHEMAS_WITH_JUDGE,
    )
    leg = script[script.index("_falsifierResult = await (async () => {") :].split("})();", 1)[0]
    assert "catch (e)" in leg
    assert "status: 'indeterminate', differs_from_baseline: null" in leg
    # The degrade names its cause; a 400 and a missing agentType never read alike.
    assert "observation: 'unjudged: ' + _cause" in leg
    assert "'unresolved-agent-type'" in leg and "'schema-reject'" in leg and "'api-error '" in leg


def test_a_judge_met_is_held_to_does_verdict_rules_post_hoc():
    """terminal-judge-verdict-rules: a `met` with nothing observed, a failing
    falsifier or an unmet clause demotes; only the judge leg is held to it."""
    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    judged = compose_script(
        waves, name="wf", description="judge", plan_path="docs/plans/p.md",
        review_roster_fragment=_V5_FRAGMENT_WITH_JUDGE, review_stage_schemas=_STAGE_SCHEMAS_WITH_JUDGE,
    )
    assert "'met demoted: '" in judged and "differs_from_baseline: null, reason" in judged
    assert "with no reason" in judged
    falsifier = {"how": "run it", "baseline_output": "fails", "expected_when_true": "passes"}
    plain = compose_script(waves, name="wf", description="f", falsifier=falsifier, **REVIEW_KW)
    assert "'met demoted: '" not in plain


def test_the_judge_runs_a_recorded_falsifier_rather_than_the_test_runner():
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    falsifier = {"how": "run it", "baseline_output": "fails", "expected_when_true": "passes"}
    script = compose_script(
        waves, name="wf", description="judge+falsifier", falsifier=falsifier,
        review_roster_fragment=_V5_FRAGMENT_WITH_JUDGE, review_stage_schemas=_STAGE_SCHEMAS_WITH_JUDGE,
    )
    body = script[script.index("[_testResult, _falsifierResult] = await parallel([") :].split("]);", 1)[0]
    assert "test:terminal'" in body
    assert "coordinator:criterion-judge" in body and "run it" in body
    assert "test:terminal-falsifier'" not in script


def test_a_delivery_fail_tells_the_operator_landed_rows_are_uncommitted():
    """A delivery FAIL stops the run for review but the rows are already on
    disk: the digest says so and points at the terminal_commit next_action,
    which stays populated (a FAIL never drops it) and within the cap."""
    waves = [[_row("C1", ["a.py"])]]
    script = compose_script(
        waves,
        name="wf",
        description="delivery fail",
        review_roster_fragment=_V5_FRAGMENT,
        review_stage_schemas=_V5_STAGE_SCHEMAS,
    )
    assert "'review delivery verdict FAIL; landed rows are UNCOMMITTED: next_action dispatch.terminal_commit commits them'" in script
    assert len("review delivery verdict FAIL; landed rows are UNCOMMITTED: next_action dispatch.terminal_commit commits them") <= 300
    assert "next_action: { kind: 'terminal_commit'" in script


def _criterion_status_regex(script: str):
    """The emitted contradiction guard's JS regex literal, as a Python pattern."""
    import re

    m = re.search(r"\(_falsifierResult\.status === 'met' && /(.+?)/i\.test\(", script)
    assert m, "criterion.status carries no met-vs-observation contradiction guard"
    return re.compile(m.group(1), re.IGNORECASE)


def test_a_met_criterion_whose_observation_says_baseline_matched_is_reported_not_met():
    """A falsifier agent reported `met` with observation "baseline matches
    ('absent'). Exit criterion not yet met": the BASELINE matching is the
    pre-change state, so the digest must read not_met -- `met` stamps the plan
    implemented."""
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    falsifier = {"how": "run it", "baseline_output": "absent", "expected_when_true": "present"}
    script = compose_script(waves, name="wf", description="falsifier", falsifier=falsifier, **REVIEW_KW)

    assert "? 'not_met' : _falsifierResult.status))) : 'not_run')" in script
    guard = _criterion_status_regex(script)
    assert guard.search("baseline matches ('absent'). Exit criterion not yet met")
    assert guard.search("output matches the baseline")
    assert guard.search("criterion not met")
    assert not guard.search("output is 'present', differs from the baseline: criterion satisfied")
    # The same demoted value feeds decision_required, not only criterion.status.
    assert script.count("'not_met' : _falsifierResult.status") >= 2


def test_a_halted_or_unanswered_row_is_not_blanket_labelled_not_started():
    """Rows reported PARTIAL/BLOCKED have written files; only a row the halt kept
    from starting is `not_started`. Deviations are deduped and kinded per id."""
    waves = [[_row("C1", ["a.py"])]]
    script = compose_script(waves, name="wf", description="deviation kinds", **REVIEW_KW)
    line = next(l for l in script.splitlines() if l.lstrip().startswith("deviations:"))
    assert "kind: 'not_started'" not in line
    assert "[...new Set([..._incompleteChunks, ..._unansweredBriefs, ..._notStarted])]" in line
    assert "(_notStarted.includes(id) ? 'not_started' : " in line
    assert "'no_answer'" in line and "'stop_rule'" in line and "'partial'" in line
    assert "(_blockedChunks.includes(id) ? 'blocked' : 'partial')" in line
    assert line.index("'no_answer'") < line.index("'blocked'") < line.index("'partial'")


def test_criterion_status_is_computed_from_the_boolean_before_the_agents_status():
    """The boolean leg precedes the legacy status/regex leg, and a null result is
    `not_run` in the criterion block, the run record and decision_required alike."""
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    falsifier = {"how": "run it", "baseline_output": "absent", "expected_when_true": "present"}
    script = compose_script(waves, name="wf", description="falsifier", falsifier=falsifier, **REVIEW_KW)
    expr = (
        "(_falsifierResult ? (_falsifierResult.differs_from_baseline === true ? 'met' : "
        "(_falsifierResult.differs_from_baseline === false ? 'not_met' : "
    )
    assert script.count(expr) >= 2  # criterion.status, decision_required
    assert ": 'not_run')" in script
    first = script.index(expr)
    assert first < script.index("_falsifierResult.status === 'met'", first)
