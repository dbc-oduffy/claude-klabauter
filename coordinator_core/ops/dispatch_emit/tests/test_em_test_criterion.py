"""em_test_criterion: the declaration predicate (both arms) and the UE report parser."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit.em_test_criterion import (
    EM_OWED_TEST_CRITERION_FIELD,
    declared_test_criterion,
    em_owed_test_criterion,
    parse_ue_automation_report,
)

_CRIT = {"kind": "ue-automation", "tests": ["Proj.A", "Proj.B"]}


def _em_row(**extra):
    return {"id": "EM1", "performer": "em", "test_criterion": dict(_CRIT), **extra}


def test_field_name_is_the_pinned_manifest_key():
    assert EM_OWED_TEST_CRITERION_FIELD == "em_owed_test_criterion"


def test_declared_returns_first_em_performed_ue_row():
    rows = [{"id": "X", "writes": ["a.cpp"]}, _em_row()]
    assert declared_test_criterion(rows) == {"row": "EM1", "kind": "ue-automation", "tests": ["Proj.A", "Proj.B"]}


def test_code_writing_row_carrying_criterion_is_ignored():
    row = {"id": "C", "agent_type": "coordinator:executor", "writes": ["Src/A.cpp"],
           "performer": "em", "test_criterion": dict(_CRIT)}
    assert declared_test_criterion([row]) is None


def test_non_em_row_and_other_kind_are_ignored():
    assert declared_test_criterion([{"id": "A", "test_criterion": dict(_CRIT)}]) is None
    assert declared_test_criterion([_em_row(test_criterion={"kind": "pytest"})]) is None
    assert declared_test_criterion([{"id": "A", "performer": "em"}]) is None


def test_owed_when_cpp_only_declared():
    got = em_owed_test_criterion([_em_row()], ["Plugin/Source/A.cpp", "Plugin/Source/A.h"])
    assert got is not None and got["row"] == "EM1"


def test_python_write_keeps_todays_halt():
    assert em_owed_test_criterion([_em_row()], ["Plugin/Source/A.cpp", "tools\\x.PY"]) is None


def test_undeclared_is_none():
    assert em_owed_test_criterion([{"id": "A", "writes": ["a.cpp"]}], ["a.cpp"]) is None


def _report(tmp_path, tests):
    p = tmp_path / "index.json"
    p.write_text(json.dumps({"tests": tests}), encoding="utf-8")
    return p


def _t(path, state="Success"):
    return {"fullTestPath": path, "testDisplayName": path.rsplit(".", 1)[-1], "state": state}


def test_parser_pass(tmp_path):
    idx = _report(tmp_path, [_t("Proj.A"), _t("Proj.B"), _t("Proj.C", "Fail")])
    out = parse_ue_automation_report(idx, ["Proj.A", "B"])
    assert out["ok"] is True and out["missing"] == [] and out["failed"] == []


def test_parser_missing_and_failed(tmp_path):
    idx = _report(tmp_path, [_t("Proj.A"), _t("Proj.B", "Fail")])
    out = parse_ue_automation_report(idx, ["Proj.A", "Proj.B", "Proj.Z"])
    assert out["ok"] is False
    assert out["missing"] == ["Proj.Z"] and out["failed"] == ["Proj.B"]


def test_parser_malformed_never_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert parse_ue_automation_report(bad, ["Proj.A"])["ok"] is False
    assert parse_ue_automation_report(tmp_path / "absent.json", ["Proj.A"])["ok"] is False
    nolist = tmp_path / "n.json"
    nolist.write_text(json.dumps({"tests": 5}), encoding="utf-8")
    assert "reason" in parse_ue_automation_report(nolist, ["Proj.A"])
    assert parse_ue_automation_report(_report(tmp_path, [_t("Proj.A")]), [])["ok"] is False
