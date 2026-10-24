"""Pins the workweek-start run spec: manifest, steps, the priorities ladder and the steps decisions open."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.ceremony_common.run import decide
from coordinator_core.ceremony_common.run.contract import (
    HANDBACK_DISPOSITIONS,
    NEVER_OFFERED_DISPOSITIONS,
    Decision,
    StepResult,
)
from coordinator_core.workweek_start import run_spec as rs

REPO_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
RUN = {"session_id": "abcdef0123456789", "run_id": "20261012T000000Z-000000aaaa", "day": "2026-10-12"}
SID = "abcdef01"

SCAN = (
    "GOAL: Ship the engine\n  id: g1\n  path: state/goals/a.yaml\n  coverage: 1\n  - docs/plans/p1.md (plan)\n"
    "GOAL: Idle goal [ZERO COVERAGE]\n  id: g2\n  path: state/goals/b.yaml\n  coverage: 0\n"
)


def _ran(step_id: str, stdout: str = "") -> StepResult:
    return StepResult(id=step_id, status="ran", exit_code=0, stdout=stdout)


def _unfinished(rows: list[dict], header: dict | None = None) -> StepResult:
    return _ran(rs.STEP_UNFINISHED, json.dumps({"rows": rows, "header": header}))


def _carried(n: int) -> list[dict]:
    return [
        {"title": f"Carry {i}", "goal_path": f"state/goals/c{i}.yaml", "evidence": f"frag.md:{i}", "goal_open": True}
        for i in range(1, n + 1)
    ]


def _resolve(ledger, flags=(), envelope=None):
    env = envelope or {}
    points = rs.spec_points(env, flags, ledger)
    return points, decide.resolve(points, [], rules=rs.POINT_RULES, context={})


def _decision(res, point_id) -> Decision | None:
    return next((d for d in res.decisions if d.point_id == point_id), None)


def _entry(res, point_id):
    return next((e for e in res.judgment_entries if e["id"] == point_id), None)


def _agent_decision(titles: list[tuple[str, str]], candidates: list[list], decided_by="agent:x") -> Decision:
    return Decision(
        point_id=rs.PRIORITIES_POINT, question="q", decision="set", decided_by=decided_by, rule="",
        rationale="picked", evidence=f"e | candidates={json.dumps(candidates)}",
        payload={"priorities": [{"title": t, "evidence": e} for t, e in titles]},
    )


def _ids(steps) -> list[str]:
    return [s.id for s in steps]


def test_manifest_equals_dispatch_row_and_barewords_resolve():
    assert rs.CONSUMES_MANIFEST == ASSEMBLER_DISPATCHABLE["workweek_start_run"]
    missing = [m for m in rs.CONSUMES_MANIFEST if not (REPO_BIN / f"{m}.py").exists()]
    assert missing == []


def test_every_cli_step_is_admitted_by_the_manifest():
    decisions = [Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                          payload={"priorities": [{"title": "A", "evidence": "e"}]})]
    steps = [*rs.SPEC.front_door, *rs.SPEC.residue, *rs.finish_steps(decisions, [], run=RUN)]
    assert {s.target for s in steps if s.kind == "cli"} <= rs.CONSUMES_MANIFEST


def test_spec_shape_and_template_match_workday_start():
    assert rs.SPEC.dispatch_key == "workweek_start_run"
    assert rs.SPEC.marker_path is None
    assert rs.SPEC.launch_flags == {"--priority"}
    assert rs.SPEC.chain == ("workday-start-run", "launch")
    cli = importlib.import_module("coordinator_core.workday_start.cli")
    assert rs.SPEC.template == cli.TEMPLATE_RELPATH
    assert set(rs.SPEC.rules) == set(rs.RULES)


def test_engine_steps_have_a_cost_row_at_or_under_the_bar():
    rows = json.loads(rs.COSTS_PATH.read_text(encoding="utf-8"))["steps"]
    decisions = [Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                          payload={"priorities": [{"title": "A", "evidence": "e"}]})]
    checked = 0
    for step in [*rs.SPEC.front_door, *rs.SPEC.residue, *rs.finish_steps(decisions, [], run=RUN)]:
        if step.cost_class != "engine":
            continue
        assert step.id in rows, step.id
        assert rows[step.id]["process_ms_min"] <= 500, step.id
        checked += 1
    assert checked, "no engine-class step: step_costs.json unreadable or every step unmeasured"


def test_stage_of_raises_on_an_unknown_directive():
    assert rs.stage_of("d-plan-orphan-tiers") == "orient"
    assert rs.stage_of("d-addon-health-3") == "orient"
    with pytest.raises(ValueError):
        rs.stage_of("d-nothing")


def test_priority_flags_decide_by_rule_with_no_judgment_entry():
    points, res = _resolve([_unfinished([])], flags=["--priority", "Ship A", "--priority=Ship B"])
    d = _decision(res, rs.PRIORITIES_POINT)
    assert d.rule == "priorities-supplied" and d.decided_by == "rule"
    assert [p["title"] for p in d.payload["priorities"]] == ["Ship A", "Ship B"]
    assert _entry(res, rs.PRIORITIES_POINT) is None
    _, res_map = _resolve([_unfinished([])], flags={"--priority": ["Only"]})
    assert _decision(res_map, rs.PRIORITIES_POINT).payload["priorities"][0]["title"] == "Only"


@pytest.mark.parametrize("n", [1, 3])
def test_one_to_three_carried_lines_decide_by_rule(n):
    _, res = _resolve([_unfinished(_carried(n))])
    d = _decision(res, rs.PRIORITIES_POINT)
    assert d.rule == "priorities-carried"
    assert [p["title"] for p in d.payload["priorities"]] == [f"Carry {i}" for i in range(1, n + 1)]
    assert d.payload["priorities"][0]["evidence"] == "frag.md:1"
    assert _entry(res, rs.PRIORITIES_POINT) is None


def test_carried_line_with_a_closed_goal_is_not_carried():
    rows = _carried(2)
    rows[0]["goal_open"] = False
    points, res = _resolve([_unfinished(rows), _ran(rs.STEP_COVERAGE, SCAN)])
    assert [c["title"] for c in _decision(res, rs.PRIORITIES_POINT).payload["priorities"]] == ["Carry 2"]


def test_four_carried_lines_make_one_entry_with_them_first():
    _, res = _resolve([_unfinished(_carried(4)), _ran(rs.STEP_COVERAGE, SCAN)])
    assert _decision(res, rs.PRIORITIES_POINT) is None
    entry = _entry(res, rs.PRIORITIES_POINT)
    titles = [c["title"] for c in entry["candidates"]]
    assert titles[:4] == ["Carry 1", "Carry 2", "Carry 3", "Carry 4"]
    assert titles[4:] == ["Ship the engine", "Idle goal"]
    assert entry["dispositions"] == [{"value": "set", "resolves": []}]
    assert "payload" in entry


def test_no_prior_lines_makes_exactly_one_entry_listing_the_active_goals():
    points, res = _resolve([_unfinished([]), _ran(rs.STEP_COVERAGE, SCAN)])
    entries = [e for e in res.judgment_entries if e["id"] == rs.PRIORITIES_POINT]
    assert len(entries) == 1
    assert [c["title"] for c in entries[0]["candidates"]] == ["Ship the engine", "Idle goal"]
    assert entries[0]["candidates"][0]["goal_path"] == "state/goals/a.yaml"


def test_brief_sources_join_the_candidates_in_preference_order():
    envelope = {"directives": [
        {"id": "d-plan-orphan-tiers", "detail": "P1 authorized_orphan: docs/plans/p3.md (execution_authorized_by=x)\n"
                                                  "P2 orphan: docs/plans/p4.md"},
    ]}
    _, res = _resolve([_unfinished([]), _ran(rs.STEP_COVERAGE, SCAN)], envelope=envelope)
    entry = _entry(res, rs.PRIORITIES_POINT)
    assert [(c["title"], c["source"]) for c in entry["candidates"]] == [
        ("Ship the engine", "active-goal"), ("Idle goal", "active-goal"),
        ("p3", "p1-orphan"),
    ]


def test_candidate_from_a_step_that_did_not_run_contributes_nothing():
    failed = StepResult(id=rs.STEP_COVERAGE, status="not-composed", reason="over-bar", stdout=SCAN)
    _, res = _resolve([_unfinished([]), failed])
    assert _decision(res, rs.PRIORITIES_POINT).rule == "priorities-no-evidence"


def test_empty_candidates_fail_with_no_judgment_entry():
    _, res = _resolve([_unfinished([])])
    assert _entry(res, rs.PRIORITIES_POINT) is None
    d = _decision(res, rs.PRIORITIES_POINT)
    assert d.decision == "unresolved"
    steps = rs.finish_steps([d], [], run=RUN)
    assert steps[0].id == rs.GATE_STEP and steps[0].gates_next
    assert {s.id for s in steps[1:]} >= {rs.STEP_CHAIN, rs.STEP_HOOK}
    code, text = _run_inproc(steps[0], Path("."))
    assert code == 1 and text == "no-priority-evidence"


def _run_inproc(step, repo_root):
    module_name, _, func_name = step.target.partition(":")
    return getattr(importlib.import_module(module_name), func_name)(repo_root)


def test_no_priorities_decision_halts_the_chain():
    steps = rs.finish_steps([], [], run=RUN)
    assert steps[0].id == rs.GATE_STEP
    assert steps[-1].id == rs.STEP_CHAIN and "w_step8_commit_priorities" in _ids(steps)
    assert _run_inproc(steps[0], Path("."))[1] == "priorities-undecided"


def test_agent_title_that_is_not_a_candidate_is_undecided():
    cands = [["Real", "ev", None]]
    d = _agent_decision([("Invented", "ev")], cands)
    steps = rs.finish_steps([d], [], run=RUN)
    assert steps[0].id == rs.GATE_STEP
    assert _run_inproc(steps[0], Path("."))[1] == "priority-not-a-candidate:Invented"
    mismatch = _agent_decision([("Real", "other-evidence")], cands)
    assert rs.finish_steps([mismatch], [], run=RUN)[0].id == rs.GATE_STEP


def test_four_agent_priorities_and_a_missing_payload_are_undecided():
    cands = [[f"T{i}", f"e{i}", None] for i in range(4)]
    four = _agent_decision([(f"T{i}", f"e{i}") for i in range(4)], cands)
    assert _run_inproc(rs.finish_steps([four], [], run=RUN)[0], Path("."))[1] == "priority-count"
    bare = Decision(rs.PRIORITIES_POINT, "q", "set", "agent:x", "", "r", "e")
    assert _run_inproc(rs.finish_steps([bare], [], run=RUN)[0], Path("."))[1] == "priorities-payload-missing"


def test_agent_pick_takes_its_goal_from_the_candidate_and_is_not_scaffolded():
    d = _agent_decision([("Ship the engine", "goal-coverage-scan: state/goals/a.yaml")],
                        [["Ship the engine", "goal-coverage-scan: state/goals/a.yaml", "state/goals/a.yaml"]])
    steps = rs.finish_steps([d], [], run=RUN)
    assert not [s for s in steps if s.args[:1] == ("scaffold-goal",)]
    assert ("emit-goal-event", "--goal", "state/goals/a.yaml") in [s.args for s in steps]


def test_carried_priority_is_not_scaffolded_a_second_goal():
    _, res = _resolve([_unfinished(_carried(2))])
    steps = rs.finish_steps(res.decisions, [_unfinished(_carried(2))], run=RUN)
    assert not [s for s in steps if s.args[:1] == ("scaffold-goal",)]
    assert [s.args[2] for s in steps if s.args[:1] == ("emit-goal-event",)] == [
        "state/goals/c1.yaml", "state/goals/c2.yaml"]


def test_supplied_priorities_scaffold_with_the_launching_session_id():
    _, res = _resolve([_unfinished([])], flags=["--priority", "Ship A", "--priority", "Ship A!"])
    steps = rs.finish_steps(res.decisions, [], run=RUN)
    scaffolds = [s for s in steps if s.args[:1] == ("scaffold-goal",)]
    assert [s.id for s in scaffolds] == ["w_step7_scaffold_goal", "w_step7_scaffold_goal_2"]
    outs = [s.args[-1] for s in scaffolds]
    assert outs[0] == f"state/goals/2026-10-12-ship-a-{SID}.yaml" and outs[0] != outs[1]
    assert all(o.endswith(f"-{SID}.yaml") for o in outs)
    assert all(s.args[s.args.index("--sid-short") + 1] == SID for s in scaffolds)
    commit = next(s for s in steps if s.args[:1] == ("commit-priorities",))
    assert commit.args == ("commit-priorities", "--sid-short", SID)
    assert [s.id for s in steps if s.kind == "op"] == ["w_step9_5_git_maintenance_weekly"]


def _marker_steps(marker: str | None, header_week="2026-10-05"):
    sup = Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                   payload={"priorities": [{"title": "A", "evidence": "e"}]})
    decisions = [sup]
    if marker:
        decisions.append(Decision(rs.MARKER_POINT, "q", marker, "rule", "week-marker", "r", "e"))
    rows = [_unfinished([], {"week_starting": header_week, "last_start": None, "unset": header_week is None})]
    return rs.finish_steps(decisions, rows, run=RUN)


def test_reset_week_adds_commit_archive_reset_and_update_does_not():
    reset = _marker_steps("reset_week")
    assert "w_step8_reset_week" in _ids(reset) and "w_step8_update_in_place" not in _ids(reset)
    archive = next(s for s in reset if s.id == "w_step8_commit_archive_reset")
    assert archive.args == ("commit-archive-reset", "--prior-week-start", "2026-10-05")
    update = _marker_steps("update_in_place")
    assert "w_step8_update_in_place" in _ids(update) and "w_step8_commit_archive_reset" not in _ids(update)


def _archive_args(state_extra):
    sup = Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                   payload={"priorities": [{"title": "A", "evidence": "e"}]})
    mk = Decision(rs.MARKER_POINT, "q", "reset_week", "rule", "week-marker", "r", "e")
    state = {"week_starting": "2026-10-05", "last_start": None, "unset": False, **state_extra}
    steps = rs.finish_steps([sup, mk], [_unfinished([], state)], run=RUN)
    return next(s for s in steps if s.id == "w_step8_commit_archive_reset").args


def test_commit_archive_reset_names_each_missed_week_oldest_first_once():
    args = _archive_args({"missed_weeks": ["2026-09-28", "2026-10-05"], "missed_as_of": RUN["day"]})
    assert args == ("commit-archive-reset", "--prior-week-start", "2026-09-28", "--prior-week-start", "2026-10-05")


def test_commit_archive_reset_adds_a_header_week_the_missed_list_lacks():
    args = _archive_args({"missed_weeks": ["2026-09-28"], "missed_as_of": RUN["day"]})
    assert args[1:] == ("--prior-week-start", "2026-09-28", "--prior-week-start", "2026-10-05")


def _pairs(*weeks):
    return ("commit-archive-reset", *(a for w in weeks for a in ("--prior-week-start", w)))


def test_a_day_key_mismatch_keeps_the_stale_missed_weeks_and_adds_the_as_of_week():
    args = _archive_args({"missed_weeks": ["2026-09-28"], "missed_as_of": "2026-10-11",
                          "missed_as_of_week": "2026-10-05"})
    assert args == _pairs("2026-09-28", "2026-10-05")


def test_a_day_key_mismatch_names_every_intermediate_week():
    args = _archive_args({"missed_weeks": ["2026-09-21"], "missed_as_of": "2026-09-30",
                          "missed_as_of_week": "2026-09-28"})
    assert args == _pairs("2026-09-21", "2026-09-28", "2026-10-05")


def test_a_day_key_mismatch_without_an_as_of_week_never_narrows():
    args = _archive_args({"missed_weeks": ["2026-09-28", "2026-10-05"], "missed_as_of": "2026-10-11"})
    assert args == _pairs("2026-09-28", "2026-10-05")


def test_header_state_records_the_day_its_missed_weeks_were_computed_on(tmp_path):
    (tmp_path / rs.header.WEEK_DIR).mkdir(parents=True)
    (tmp_path / rs.header.WEEK_DIR / "HEADER.md").write_text("**Week starting:** 2026-10-05\n", encoding="utf-8")
    state = rs.header_state(tmp_path)
    assert state["missed_as_of"] == str(rs.local_day(str(tmp_path)))
    assert state["missed_as_of_week"] == rs.header.current_week_start(tmp_path, state["missed_as_of"])


def test_reset_on_an_unset_header_starts_the_week_without_an_archive_commit():
    steps = _marker_steps("reset_week", header_week=None)
    assert "w_step8_start_week" in _ids(steps) and "w_step8_commit_archive_reset" not in _ids(steps)


def test_no_marker_decision_takes_the_update_in_place_set():
    assert _ids(_marker_steps(None)) == _ids(_marker_steps("update_in_place"))


def test_chain_is_the_last_finish_step_and_priorities_gate_the_rest():
    steps = _marker_steps("reset_week")
    assert steps[-1].id == rs.STEP_CHAIN and steps[-1].cost_class == "launch"
    assert steps[-1].target == "workday-start-run" and steps[-1].args == ("launch",)
    assert _ids(steps).index("w_step7_write_fragment") < _ids(steps).index("w_step8_reset_week")


def test_week_marker_rule_reads_the_header_and_defers_when_it_cannot():
    unset = {"week_starting": None, "last_start": None, "unset": True}
    fresh = {"week_starting": "2026-10-05", "last_start": "2026-10-05", "unset": False}
    pts = rs.spec_points({}, [], [_unfinished([], unset)])
    assert [p["id"] for p in pts] == [rs.PRIORITIES_POINT, rs.MARKER_POINT]
    _, res = _resolve([_unfinished([], unset)])
    assert _decision(res, rs.MARKER_POINT).decision == "reset_week"
    _, res = _resolve([_unfinished([], fresh)])
    assert _decision(res, rs.MARKER_POINT).decision == "update_in_place"
    brief_point = {"id": rs.MARKER_POINT, "question": "q", "dispositions": [{"value": "reset_week"}]}
    assert rs.week_marker(brief_point, context={}) is None
    resolved = decide.resolve([brief_point], [], rules=rs.POINT_RULES, context={})
    assert resolved.judgment_entries and resolved.judgment_entries[0]["id"] == rs.MARKER_POINT
    assert rs.week_marker(brief_point, context={"header": unset}).decision == "reset_week"
    no_header = rs.spec_points({}, [], [_unfinished([], None)])
    assert [p["id"] for p in no_header] == [rs.PRIORITIES_POINT]


def test_brief_emitted_marker_point_is_not_emitted_twice():
    env = {"judgment_points": [{"id": rs.MARKER_POINT}]}
    pts = rs.spec_points(env, [], [_unfinished([], {"unset": False, "week_starting": "2026-10-05", "last_start": None})])
    assert [p["id"] for p in pts] == [rs.PRIORITIES_POINT]


def test_memo_points_are_left_for_the_day_run():
    for pid in ("j-memo-1", "j-overflow-memo"):
        point = {"id": pid, "question": "q", "evidence": "x", "dispositions": [{"value": "accept"}], "recommendation": None}
        res = decide.resolve([point], [], rules=rs.POINT_RULES, context={})
        assert res.decisions[0].decision == "left-for-day-run" and res.decisions[0].rule == "memo-left-for-day-run"


def test_no_entry_offers_discard_or_a_handback():
    _, res = _resolve([_unfinished(_carried(4)), _ran(rs.STEP_COVERAGE, SCAN)])
    banned = HANDBACK_DISPOSITIONS | NEVER_OFFERED_DISPOSITIONS
    for entry in res.judgment_entries:
        assert not {d["value"] for d in entry["dispositions"]} & banned
    groups = rs.agent_groups({}, [], res)
    assert [g["id"] for g in groups] == ["judgment"] and groups[0]["entries"][0]["inputs"]["candidates"]
    assert rs.agent_groups({}, [], decide.Resolution([], [], [])) == []


def test_conditional_scratch_apply_needs_the_hold_pending_capability():
    sup = Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                   payload={"priorities": [{"title": "A", "evidence": "e"}]})
    with_cap = [_ran(rs.STEP_SCRATCH_DRY, json.dumps({"capabilities": ["hold_pending_marker"]}))]
    assert rs.finish_steps([sup], with_cap, run=RUN)[0].id == rs.STEP_SCRATCH_APPLY
    without = [_ran(rs.STEP_SCRATCH_DRY, json.dumps({"capabilities": []}))]
    assert rs.STEP_SCRATCH_APPLY not in _ids(rs.finish_steps([sup], without, run=RUN))
    assert rs.STEP_SCRATCH_APPLY not in _ids(rs.finish_steps([sup], [], run=RUN))


def test_missing_session_id_fails_the_gate():
    sup = Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                   payload={"priorities": [{"title": "A", "evidence": "e"}]})
    steps = rs.finish_steps([sup], [], run={"day": "2026-10-12"})
    assert _run_inproc(steps[0], Path("."))[1] == "no-session-id"


def test_gap_steps_are_over_bar_and_name_the_body_sentence():
    gaps = [s for s in rs.SPEC.residue if s.target == "engine-gap"]
    assert {s.id for s in gaps} >= set(rs.ENGINE_GAP_STEPS)
    assert all(s.cost_class == "over-bar" and s.detail.startswith("not-composed: engine-gap") for s in gaps)


def test_header_steps_run_end_to_end_under_tmp_path(tmp_path):
    assert rs.bootstrap_step(tmp_path)[1] == "created HEADER.md"
    assert rs.bootstrap_step(tmp_path)[1] == ""
    state = rs.header_state(tmp_path)
    assert state["unset"] is True and state["week_starting"] is None

    steps = rs.finish_steps(
        [Decision(rs.PRIORITIES_POINT, "q", "set", "rule", "priorities-supplied", "r", "e",
                  payload={"priorities": [{"title": "Ship A", "evidence": "e"}]}),
         Decision(rs.MARKER_POINT, "q", "reset_week", "rule", "week-marker", "r", "e")],
        [_unfinished([], state)], run=RUN,
    )
    for step in steps:
        if step.kind == "inproc":
            assert _run_inproc(step, tmp_path)[0] == 0, step.id
    text = (tmp_path / "state/week-changelog/HEADER.md").read_text(encoding="utf-8")
    assert "**Week starting:** 2026-10-12" in text
    fragment = (tmp_path / f"state/week-changelog/HEADER.priorities.{SID}.md").read_text(encoding="utf-8")
    assert fragment == f"- [ ] Ship A — state/goals/2026-10-12-ship-a-{SID}.yaml\n"

    code, out = rs.unfinished_step(tmp_path)
    rows = json.loads(out)["rows"]
    assert code == 0 and rows[0]["title"] == "Ship A" and rows[0]["goal_open"] is False
    assert rows[0]["evidence"].endswith(f"HEADER.priorities.{SID}.md:1")

    goal = tmp_path / "state/goals/2026-10-12-ship-a-x.yaml"
    goal.parent.mkdir(parents=True)
    goal.write_text("status: active\n", encoding="utf-8")
    (tmp_path / f"state/week-changelog/HEADER.priorities.{SID}.md").write_text(
        f"- [ ] Ship A — {goal.relative_to(tmp_path).as_posix()}\n", encoding="utf-8")
    assert json.loads(rs.unfinished_step(tmp_path)[1])["rows"][0]["goal_open"] is True


def test_reset_week_step_archives_and_update_step_marks(tmp_path):
    header = rs.header
    header.bootstrap(tmp_path)
    rs._start_week(tmp_path, today="2026-10-05")
    daily = tmp_path / "state/week-changelog/2026-10-06-box.md"
    daily.write_text("d\n", encoding="utf-8")
    code, text = _run_inproc(rs._step("x", "inproc", rs._bound("reset", today="2026-10-12"), (), stage="s", detail="d"), tmp_path)
    assert (code, text) == (0, "archived week 2026-10-05")
    assert (tmp_path / "archive/week-changelogs/2026-10-05/2026-10-06-box.md").is_file()
    assert rs.header_state(tmp_path)["week_starting"] == "2026-10-12"
    _run_inproc(rs._step("y", "inproc", rs._bound("update", today="2026-10-13"), (), stage="s", detail="d"), tmp_path)
    assert rs.header_state(tmp_path)["last_start"] == "2026-10-13"


def test_start_week_writes_the_week_anchor_not_today(tmp_path):
    rs.header.bootstrap(tmp_path)
    (tmp_path / "archive/week-changelogs/2026-09-28").mkdir(parents=True)
    code, text = rs._start_week(tmp_path, today="2026-10-07")
    assert (code, text) == (0, "week starts 2026-10-05")
    assert rs.header_state(tmp_path)["week_starting"] == "2026-10-05"


def test_marker_point_names_missed_weeks_and_catch_up_when_no_release_record():
    state = {"week_starting": "2026-09-28", "last_start": None, "unset": False,
             "missed_weeks": ["2026-09-28"], "release_recorded": False}
    evidence = rs._marker_point(state)["evidence"]
    assert "missed weeks: 2026-09-28" in evidence and "/workweek-complete --catch-up" in evidence
    released = rs._marker_point({**state, "release_recorded": True})["evidence"]
    assert "missed weeks: 2026-09-28" in released and "--catch-up" not in released
    assert "missed weeks" not in rs._marker_point({**state, "missed_weeks": []})["evidence"]


def test_bound_target_rejects_an_unknown_name():
    with pytest.raises(AttributeError):
        getattr(rs, "nothing~e30")
    with pytest.raises(AttributeError):
        getattr(rs, "plain_missing")


def test_receipt_has_decided_priorities_and_no_narration():
    _, res = _resolve([_unfinished(_carried(2)), _ran(rs.STEP_EXPIRED_GATES, "plan-x gate expired\n"),
                       _ran(rs.STEP_COVERAGE, SCAN)])
    rows = [_unfinished(_carried(2)), _ran(rs.STEP_EXPIRED_GATES, "plan-x gate expired\n"),
            _ran(rs.STEP_COVERAGE, SCAN), _ran(rs.STEP_HOOK, "hook ok\n")]
    text = rs.render(rs.SPEC, rows, res.decisions, [], {}, None, facts={})
    assert "### Decided" in text and "Parked for you" not in text
    assert "- [ ] Carry 1" in text and "### Post-ceremony hook" in text
    assert "expired plan gates: plan-x gate expired" in text
    assert "goal with no in-flight work: Idle goal" in text
    assert "### Parked for you" not in text and "Next up" not in text
