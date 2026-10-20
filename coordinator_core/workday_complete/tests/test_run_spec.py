"""The workday-complete run spec: dispatch-row parity, brief coverage, rules, groups, finish steps."""
from __future__ import annotations

import importlib
import itertools
import json
from pathlib import Path

import pytest

from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.ceremony_common.run import decide, envelope as envelope_mod
from coordinator_core.ceremony_common.run.contract import (
    HANDBACK_DISPOSITIONS,
    NARRATION_DENY_LIST,
    Decision,
    StepResult,
)
from coordinator_core.ceremony_common.run.receipt import check_narration
from coordinator_core.ops.requirement_register import StallReport
from coordinator_core.workday_complete import brief, run_spec

BIN_DIR = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
GOALS = {"today": [{"goal_id": "g1", "text": "ship it"}], "stale": []}
NO_GOALS = {"today": [], "stale": []}
AMBIGUOUS = {"ambiguous": True, "evidence": "src/a.py"}
CLEAN = {"ambiguous": False, "evidence": ""}
COMBOS = list(itertools.product((CLEAN, AMBIGUOUS), (NO_GOALS, GOALS)))


def _directives(verdict, goals, **kw):
    return brief._build_directives({}, goals, verdict, **kw)


def _points(verdict, goals):
    return brief._build_judgment_points(goals, verdict, register_stall=StallReport(stale_rows=["row"]))


def _row(step_id, status="ran", stdout="", exit_code=0):
    return StepResult(id=step_id, status=status, exit_code=exit_code, stdout=stdout)


def _decision(point_id, value, payload=None):
    return Decision(point_id, "q", value, "agent:coordinator:apm", "", "why", "ev", payload=payload)


def _all_steps(flags=None):
    steps = list(run_spec.front_door(flags)) + list(run_spec.residue(flags))
    for verdict, goals in COMBOS:
        env = {"directives": _directives(verdict, goals)}
        steps += envelope_mod.steps_from_envelope(env, stage_of=run_spec.stage_of, costs=run_spec.COSTS)
    return steps


def test_manifest_equals_dispatch_row_and_barewords_resolve():
    assert run_spec.CONSUMES_MANIFEST == ASSEMBLER_DISPATCHABLE["workday_complete_run"]
    for bareword in run_spec.CONSUMES_MANIFEST:
        assert any((BIN_DIR / f"{bareword}{ext}").is_file() for ext in (".py", ".cmd", "")), bareword


def test_every_cli_step_target_is_in_the_manifest():
    for step in _all_steps():
        if step.kind == "cli" and step.target != run_spec.NOT_COMPOSED_TARGET:
            assert step.target in run_spec.CONSUMES_MANIFEST, step.id


@pytest.mark.parametrize("verdict,goals", COMBOS)
def test_every_brief_directive_maps_to_a_stage(verdict, goals):
    for kw in ({}, {"for_date": "2026-10-11", "only_mode": True, "scope_summary": "x"}):
        for d in _directives(verdict, goals, **kw):
            assert run_spec.stage_of(d["id"]) in (
                run_spec.STAGE_LAUNCH, run_spec.STAGE_FINISH, run_spec.STAGE_FINISH_BUILT
            ), d["id"]


def test_stage_of_raises_on_an_unknown_id():
    with pytest.raises(ValueError):
        run_spec.stage_of("d_not_a_directive")


@pytest.mark.parametrize("verdict,goals", COMBOS)
@pytest.mark.parametrize("only", (False, True))
def test_every_judgment_point_lands_in_exactly_one_place(verdict, goals, only):
    directives = _directives(verdict, goals)
    points = _points(verdict, goals)
    res = decide.resolve(
        points, directives, rules=run_spec.RULE_TABLE, context={"flags": {"--only": only}}
    )
    placed = (
        [d.point_id for d in res.decisions]
        + [p["id"] for p in res.observations]
        + [e["id"] for e in res.judgment_entries]
    )
    assert sorted(placed) == sorted(p["id"] for p in points)
    assert len(placed) == len(set(placed))
    entry_ids = {e["id"] for e in res.judgment_entries}
    expected = ({run_spec.DIRTY_TREE_POINT} if verdict is AMBIGUOUS else set()) | (
        {run_spec.CLOSEOUT_POINT} if goals is GOALS else set()
    )
    assert entry_ids == expected
    for entry in res.judgment_entries:
        offered = {d["value"] for d in entry["dispositions"]}
        assert "discard" not in offered
        assert not offered & HANDBACK_DISPOSITIONS
    assert "jp_requirement_register_stall" in [p["id"] for p in res.observations]


def test_rules_decide_the_rule_points():
    points = {p["id"]: p for p in _points(CLEAN, NO_GOALS)}
    ctx = {"flags": {}}
    assert run_spec.only_mode(points[run_spec.CLUSTERING_POINT], context=ctx) is None
    skipped = run_spec.only_mode(points[run_spec.CLUSTERING_POINT], context={"flags": {"only": True}})
    assert skipped.decision == "skip_only_mode" and skipped.rule == "only-mode"
    assert run_spec.only_mode(points[run_spec.ANALYST_POINT], context={"flags": {"only": True}}) is None
    cap = run_spec.backfill_cap(points[run_spec.BACKFILL_CAP_POINT], context=ctx)
    assert cap.decision == "backfill_all" and "count flag" in cap.rationale
    assert "14-day" not in cap.rationale and "lookback window" in cap.rationale
    by_rule = {
        pid: decide.resolve([points[pid]], [], rules=run_spec.RULE_TABLE, context=ctx).decisions[0]
        for pid in (run_spec.ANALYST_POINT, run_spec.CLUSTERING_POINT, run_spec.HEALTH_LEDGER_POINT)
    }
    assert by_rule[run_spec.ANALYST_POINT].decision == "dispatch"
    assert by_rule[run_spec.CLUSTERING_POINT].decision == "dispatch_if_multi_entry_chains"
    assert by_rule[run_spec.HEALTH_LEDGER_POINT].decision == "add_unaudited_rows"
    assert {"only-mode", "backfill-cap", decide.RECOMMENDATION} <= set(run_spec.RULES)


def test_only_drops_the_two_sweeps_and_the_archive_audit():
    ids = lambda flags: [s.id for s in run_spec.residue(flags)]  # noqa: E731
    assert {"day_coverage_sweep", "baton_drift_sweep"} <= set(ids({}))
    assert not {"day_coverage_sweep", "baton_drift_sweep"} & set(ids({"--only": True}))
    env = {"directives": _directives(CLEAN, NO_GOALS, for_date="2026-10-11", only_mode=True)}
    res = decide.Resolution([], [], [])
    assert "archive-audit" not in {g["id"] for g in run_spec.agent_groups(env, [], res)}
    env_full = {"directives": _directives(CLEAN, NO_GOALS)}
    assert "archive-audit" in {g["id"] for g in run_spec.agent_groups(env_full, [], res)}


def test_front_door_splices_the_launch_flags():
    flags = {"--for-date": "2026-10-11", "--only": True, "--scope-summary": "- wrapped", "--summary": "s"}
    grant, check, assemble = run_spec.front_door(flags)
    assert grant.best_effort and grant.id == run_spec.GRANT_STEP_ID
    assert check.gates_next and check.args == ("check-cross-machine", "--for-date 2026-10-11 --only s")
    assert assemble.id == run_spec.SPEC.envelope_step
    assert assemble.args == ("brief", "--for-date", "2026-10-11", "--only", "--scope-summary=- wrapped")
    assert run_spec.front_door({})[2].args == ("brief",)


def test_every_engine_step_has_a_cost_row_at_or_under_the_bar():
    rows = json.loads(run_spec.STEP_COSTS_PATH.read_text(encoding="utf-8"))["steps"]
    steps = _all_steps()
    steps += run_spec.finish_steps(
        [_decision(run_spec.DIRTY_TREE_POINT, "adopt_commit")],
        [_row("check_auto_memory_drained", "failed", exit_code=1)],
        run={},
    )
    engine = [s for s in steps if s.cost_class == "engine"]
    assert engine
    for step in engine:
        row = rows.get(step.id.removesuffix("_recheck"))
        assert row is not None and row["basis"] == "measured", step.id
        assert row["process_ms_median"] <= 500, step.id


def test_memory_drain_group_follows_the_exit_code():
    env = {"directives": []}
    res = decide.Resolution([], [], [])
    drained = [_row("check_auto_memory_drained", "failed", "tasks/x.md\n", exit_code=1)]
    groups = {g["id"]: g for g in run_spec.agent_groups(env, drained, res)}
    assert groups["memory-drain"]["entries"][0]["inputs"]["residual"] == ["tasks/x.md"]
    clean = [_row("check_auto_memory_drained")]
    assert "memory-drain" not in {g["id"] for g in run_spec.agent_groups(env, clean, res)}
    recheck = run_spec.finish_steps([], drained, run={})
    assert [s.id for s in recheck] == ["check_auto_memory_drained_recheck"]
    assert not recheck[0].best_effort
    assert run_spec.finish_steps([], clean, run={}) == []


def test_groups_follow_the_decisions_and_the_scan():
    env = {"directives": _directives(CLEAN, NO_GOALS, for_date="2026-10-11", scope_summary="s")}
    decisions = [
        _decision(run_spec.ANALYST_POINT, "dispatch"),
        _decision(run_spec.CLUSTERING_POINT, "dispatch_if_multi_entry_chains"),
        _decision(run_spec.HEALTH_LEDGER_POINT, "add_unaudited_rows"),
    ]
    scan = "2026-10-09\t3\tb1\tt1\n2026-10-08\t2\tb0\tt0\nnot a row\n"
    rows = [_row("d_step3_5_backfill_scan", stdout=scan), _row("d_step6_archive_audit_completions", stdout="{cut")]
    groups = {g["id"]: g for g in run_spec.agent_groups(env, rows, decide.Resolution(decisions, [], []))}
    assert [e["key"] for e in groups["backfill-summary"]["entries"]] == ["day:2026-10-08", "day:2026-10-09"]
    assert groups["daily-summary"]["entries"][0]["key"] == "day:2026-10-11"
    assert groups["clustering"]["entries"][0]["key"] == "chains:2026-10-11"
    assert len(groups["health-ledger"]["entries"]) == 1
    skipped = [_decision(run_spec.ANALYST_POINT, "skip_no_new_work")]
    assert "daily-summary" not in {
        g["id"] for g in run_spec.agent_groups(env, [], decide.Resolution(skipped, [], []))
    }


def test_clustering_makes_one_entry_per_multi_entry_chain_without_a_narrative():
    def entry(chain, narrative=None):
        fm = {"created": "2026-10-11", "chain": chain}
        if narrative:
            fm["narrative"] = narrative
        return {"path": f"archive/{chain}-{id(fm)}.md", "frontmatter": fm}

    listing = [entry("a"), entry("a"), entry("b"), entry("c"), entry("c", "done")]
    env = {"directives": _directives(CLEAN, NO_GOALS, for_date="2026-10-11")}
    res = decide.Resolution([_decision(run_spec.CLUSTERING_POINT, "dispatch_if_multi_entry_chains")], [], [])
    rows = [_row("d_step6_archive_audit_completions", stdout=json.dumps(listing))]
    entries = run_spec.agent_groups(env, rows, res)[0]["entries"]
    assert [e["key"] for e in entries] == ["chain:a"]


def test_record_decision_runs_goal_close_day_once_with_its_payload():
    payload = {"g1": "done", "g2": "dropped"}
    steps = run_spec.finish_steps([_decision(run_spec.CLOSEOUT_POINT, "record", payload)], [], run={})
    assert [s.id for s in steps] == ["d_goal_close_day"]
    assert steps[0].args == ("--decisions", json.dumps(payload, sort_keys=True))
    assert steps[0].args[1] != "{}"
    env_steps = envelope_mod.steps_from_envelope(
        {"directives": _directives(CLEAN, GOALS)}, stage_of=run_spec.stage_of, costs=run_spec.COSTS
    )
    runnable = [s.id for s in env_steps if s.stage in run_spec.SPEC.launch_stages + run_spec.SPEC.finish_stages]
    assert "d_goal_close_day" not in runnable
    assert run_spec.finish_steps([_decision(run_spec.CLOSEOUT_POINT, "skip")], [], run={}) == []


@pytest.mark.parametrize("payload", (None, {}, {"g1": "maybe"}, ["g1"]))
def test_record_without_a_usable_payload_is_a_refusal(payload):
    steps = run_spec.finish_steps([_decision(run_spec.CLOSEOUT_POINT, "record", payload)], [], run={})
    assert [s.id for s in steps] == ["refuse_goal_closeout"]
    assert steps[0].gates_next and steps[0].on_exit == {1: run_spec.RULE_REFUSE}
    assert run_spec.refuse_decision(steps[0], None, repo_root=".", run_dir=".") == (
        f"payload-invalid:{run_spec.CLOSEOUT_POINT}"
    )


def test_adopt_commit_is_a_gating_commit_step():
    steps = run_spec.finish_steps([_decision(run_spec.DIRTY_TREE_POINT, "adopt_commit")], [], run={})
    assert [s.id for s in steps] == ["adopt_dirty_tree"]
    assert steps[0].gates_next and steps[0].kind == "inproc"
    assert steps[0].target == run_spec.ADOPT_COMMIT_TARGET
    module, _, func = steps[0].target.partition(":")
    assert callable(getattr(importlib.import_module(module), func))


@pytest.mark.parametrize("disposition", ("attribute_to_session",))
def test_dirty_tree_decision_has_no_step_to_carry_it(disposition):
    decisions = [
        _decision(run_spec.CLOSEOUT_POINT, "record", {"g1": "done"}),
        _decision(run_spec.DIRTY_TREE_POINT, disposition),
    ]
    steps = run_spec.finish_steps(decisions, [], run={})
    assert steps[0].id == "refuse_dirty_tree" and steps[0].gates_next
    assert run_spec.refuse_decision(steps[0], None, repo_root=".", run_dir=".") == (
        f"no-step-carries:{disposition}"
    )
    assert run_spec.refuse(".")[0] == 1
    assert run_spec.RULES[run_spec.RULE_REFUSE] is run_spec.refuse_decision


def test_stranded_batons_are_a_failed_row_with_the_count():
    rows = [_row("baton_drift_sweep", stdout="total_live=9\nstranded=2\nheld=1\n")]
    steps = run_spec.finish_steps([], rows, run={})
    assert [s.id for s in steps] == ["baton_drift_stranded"]
    assert steps[0].args == ("stranded=2",)
    assert run_spec.finish_steps([], [_row("baton_drift_sweep", stdout="stranded=0\n")], run={}) == []


def test_scratch_apply_needs_the_hold_pending_marker_capability():
    def apply_ids(stdout):
        rows = [_row("fleet_scratch_hygiene", stdout=stdout)]
        return [s.id for s in run_spec.finish_steps([], rows, run={"repo_root": "r"})]

    summary = [{"kind": "purge"}, {"summary": True, "capabilities": ["hold_pending_marker"]}]
    assert apply_ids(json.dumps(summary)) == ["fleet_scratch_hygiene_apply"]
    assert apply_ids(json.dumps({"summary": True, "capabilities": []})) == []
    assert apply_ids("not json") == []


def test_not_composed_entries_carry_their_fixed_reasons():
    gaps = [s for s in run_spec.residue({}) if s.target == run_spec.NOT_COMPOSED_TARGET]
    assert {s.id for s in gaps} == set(run_spec._GAP_REASONS)
    for step in gaps:
        assert step.cost_class == "over-bar" and step.detail.startswith("not-composed: ")


def test_receipt_sections_state_no_action_and_no_gone_lines():
    for heading, condition, source in run_spec.RECEIPT_SECTIONS:
        check_narration(heading)
        assert condition == "always" or condition in run_spec.receipt_facts([])
        assert source.partition(":")[0] in ("step", "fact")
    headings = " ".join(h for h, _, _ in run_spec.RECEIPT_SECTIONS).lower()
    for gone in ("consolidated", "daily review", "reconcile", "week-changelog", "merge", "parked"):
        assert gone not in headings
    assert not any(p.lower() in headings for p in NARRATION_DENY_LIST)


def test_receipt_facts_report_by_exception():
    quiet = run_spec.receipt_facts([_row("day_coverage_sweep", stdout="orphaned=0\n")], {})
    assert not any(v for k, v in quiet.items())
    rows = [
        _row("day_coverage_sweep", stdout="orphaned=2\n"),
        _row("baton_drift_sweep", stdout="stranded=1\n"),
        _row("d_step10_weekly_staleness", stdout="STALE: weekly\n"),
        _row("d_step10_5_post_command_hook", stdout="hook says x\n"),
        _row("d_step1_validate", "failed", exit_code=1),
        _row("check_auto_memory_drained", "failed", "tasks/x.md\n", exit_code=1),
    ]
    facts = run_spec.receipt_facts(rows, {"--for-date": "2026-10-11", "--only": True, "--summary": "s"})
    assert not facts["coverage_gaps"] and not facts["baton_stranded"]
    assert facts["weekly_stale"] and facts["hook_line"] and facts["validate_failed"]
    assert facts["memory_residue"] == "tasks/x.md" and "2026-10-11" in facts["day_summary"]
    assert run_spec.receipt_facts(rows, {})["coverage_gaps"]


def test_spec_declares_the_workday_start_template_and_a_closed_flag_set():
    spec = run_spec.SPEC
    assert spec.name == "workday-complete" and spec.dispatch_key == "workday_complete_run"
    assert spec.marker_path is None and spec.chain is None and spec.render is None
    assert spec.launch_flags == {
        "--for-date",
        "--only",
        "--scope-summary",
        "--summary",
        "--catch-up",
        "--conclude",
    }
    assert spec.template == "workflows/workday-start.mjs"
    assert spec.spec_points({}, {}, []) == []
