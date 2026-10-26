"""Parse and fold cases for the objective contract leaf; no input yields an indeterminate status."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import objective_contract as oc

AXES = [oc.Axis(id="a"), oc.Axis(id="b")]


def _line(axis, ok, **kw):
    return json.dumps({"axis": axis, "kind": "measure", "command": "c", "base": 5, "now": 1,
                       "threshold": 2, "comparator": "<=", "pass": ok, "evidence": "e", **kw})


def _summary(ok, failing):
    return json.dumps({"summary": True, "pass": ok, "failing": failing})


def _out(*lines):
    return "\n".join(lines) + "\n"


def test_all_pass_parses_clean():
    res = oc.parse_falsifier_lines(_out(_line("a", True), _line("b", True), _summary(True, [])), AXES)
    assert [r.passed for r in res] == [True, True]
    assert res[0].to_line()["pass"] is True


def test_failing_axis_with_agreeing_summary():
    res = oc.parse_falsifier_lines(_out(_line("a", True), _line("b", False), _summary(False, ["b"])), AXES)
    assert [r.passed for r in res] == [True, False]


def test_non_json_line_fails_every_axis():
    res = oc.parse_falsifier_lines(_out(_line("a", True), "garbage", _summary(True, [])), AXES)
    assert all(not r.passed and r.reason == oc.REASON_UNPARSEABLE for r in res)


def test_missing_axis_fails_that_axis():
    res = oc.parse_falsifier_lines(_out(_line("a", True), _summary(False, ["b"])), AXES)
    assert res[0].passed is True
    assert res[1].passed is False and res[1].reason == oc.REASON_MISSING_AXIS


def test_duplicate_axis_fails_that_axis():
    res = oc.parse_falsifier_lines(_out(_line("a", True), _line("a", True), _line("b", True), _summary(False, ["a"])), AXES)
    assert res[0].passed is False and res[0].reason == oc.REASON_DUPLICATE_AXIS
    assert res[1].passed is True


def test_summary_pass_disagreeing_with_axis_lines_is_not_met():
    res = oc.parse_falsifier_lines(_out(_line("a", True), _line("b", False), _summary(True, [])), AXES)
    assert all(not r.passed and r.reason == oc.REASON_SUMMARY_DISAGREES for r in res)


def test_summary_fail_with_all_axes_green_is_not_met():
    res = oc.parse_falsifier_lines(_out(_line("a", True), _line("b", True), _summary(False, ["a"])), AXES)
    assert all(not r.passed for r in res)


def test_missing_summary_and_empty_stdout_fail():
    assert not any(r.passed for r in oc.parse_falsifier_lines(_out(_line("a", True), _line("b", True)), AXES))
    assert not any(r.passed for r in oc.parse_falsifier_lines("", AXES))


def test_non_bool_pass_fails():
    res = oc.parse_falsifier_lines(_out(_line("a", "yes"), _line("b", True), _summary(False, ["a"])), AXES)
    assert res[0].passed is False and res[0].reason == oc.REASON_BAD_PASS


def _check(wave, passes, kind=oc.LINE_CHECK):
    return {"wave": wave, "iteration": wave, "kind": kind,
            "axes": [{"axis": k, "pass": v, "now": 1} for k, v in passes.items()]}


def test_fold_empty_and_calibration_only_are_not_met_with_every_axis_unmet():
    for loop in ([], None, [_check(0, {"a": True, "b": True}, kind=oc.LINE_CALIBRATION)]):
        v = oc.close_verdict(loop, ["a", "b"])
        assert v["status"] == oc.STATUS_NOT_MET and v["unmet"] == ["a", "b"]


def test_fold_reads_last_check_only_and_skips_calibration():
    loop = [_check(1, {"a": False, "b": False}), _check(2, {"a": True, "b": True}),
            _check(0, {"a": False}, kind=oc.LINE_CALIBRATION)]
    assert oc.close_verdict(loop) == {"status": oc.STATUS_DONE, "unmet": []}
    loop = [_check(1, {"a": True, "b": True}), _check(2, {"a": True, "b": False})]
    assert oc.close_verdict(loop) == {"status": oc.STATUS_NOT_MET, "unmet": ["b"]}


def test_fold_declared_axis_absent_from_last_check_is_unmet():
    v = oc.close_verdict([_check(1, {"a": True})], ["a", "b"])
    assert v["status"] == oc.STATUS_NOT_MET and v["unmet"] == ["b"]


def test_fold_status_is_only_done_or_not_met():
    cases = [[], [_check(1, {})], [_check(1, {"a": None})], [{"kind": "weird"}], [_check(1, {"a": True})]]
    for loop in cases:
        assert oc.close_verdict(loop)["status"] in (oc.STATUS_DONE, oc.STATUS_NOT_MET)
    assert oc.close_verdict([_check(1, {})])["status"] == oc.STATUS_NOT_MET


def test_loop_result_met_first_pass_flag_separates_loop_use():
    first = oc.objective_loop_result([_check(1, {"a": True})])
    assert first["outcome"] == oc.OUTCOME_MET and first["first_pass"] is True and first["unmet_axes"] == []
    saved = oc.objective_loop_result(
        [_check(1, {"a": False}), _check(2, {"a": True}) | {"check_cost_usd": 0.0, "check_llm_calls": 0}])
    assert saved["outcome"] == oc.OUTCOME_MET and saved["first_pass"] is False
    assert saved["iterations"][1]["check_llm_calls"] == 0 and "check_cost_usd" in saved["iterations"][1]


def test_outcome_enum_and_only_three_terminal():
    assert oc.OUTCOMES == ("met", "impossible", "doctrine_conflict", "exhausted")
    assert {o for o in oc.OUTCOMES if oc.is_terminal(o)} == {"met", "impossible", "doctrine_conflict"}
    assert not oc.is_terminal(oc.OUTCOME_EXHAUSTED) and not oc.is_terminal("closed_partial")


def test_loop_result_unmet_is_exhausted_nonterminal_never_partial():
    res = oc.objective_loop_result([_check(1, {"a": False})], oc.Calibration(verdict="ok"))
    assert res["outcome"] == oc.OUTCOME_EXHAUSTED and res["unmet_axes"] == ["a"]
    assert not oc.is_terminal(res["outcome"]) and "closed_partial" not in oc.OUTCOMES
    assert res["calibration"]["verdict"] == "ok"


def test_loop_result_explicit_exit_and_rethinks():
    res = oc.objective_loop_result(
        [_check(1, {"a": False})], outcome=oc.OUTCOME_IMPOSSIBLE, exit_reason=oc.EXIT_IMPOSSIBLE,
        rethinks=[oc.Rethink(trigger="attempts_n", approach_path="p.md", verdict="impossible")])
    assert res["outcome"] == oc.OUTCOME_IMPOSSIBLE
    assert res["rethinks"][0]["trigger"] == "attempts_n"


def test_objective_roundtrip_and_defaults():
    obj = oc.Objective(verbatim="v", falsifier_script="s.py", axes=[oc.Axis(id="a")])
    back = oc.Objective.from_json(obj.to_json())
    assert back == obj and back.attempts_n == oc.MAX_REACHER_ITERATIONS
    assert back.falsifier_sha256 is None and back.pinned_at is None
