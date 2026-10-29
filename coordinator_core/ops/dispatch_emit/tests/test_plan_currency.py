"""plan_currency.assess and the compose-time stale-resume-plan seam, both sides of the decent decision."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, plan_currency


def _fake_gate(verdict, classes=None, message=""):
    def fake(raw, root):
        return {"verdict": verdict, "classes": classes or {}, "message": message}, ""
    return fake


def test_a_prepped_plan_is_decent(monkeypatch, tmp_path):
    monkeypatch.setattr("coordinator_core.ops.plan_prep_gate.prep_gate_with_text", _fake_gate("PREPPED"))
    assert plan_currency.assess(tmp_path, "docs/plans/p.md") == {"decent": True, "findings": []}


def test_a_not_prepped_plan_is_not_decent_and_names_its_failing_class(monkeypatch, tmp_path):
    classes = {"SPINE": {"status": "DEFECT", "detail": "no rows"}, "OTHER": {"status": "OK"}}
    monkeypatch.setattr(
        "coordinator_core.ops.plan_prep_gate.prep_gate_with_text", _fake_gate("NOT-PREPPED", classes, "msg")
    )
    out = plan_currency.assess(tmp_path, "docs/plans/p.md")
    assert out["decent"] is False
    assert out["findings"] == ["SPINE: no rows", "msg"]


def test_an_unreadable_plan_is_not_decent_rather_than_raising(tmp_path):
    out = plan_currency.assess(tmp_path, "docs/plans/missing.md")
    assert out["decent"] is False and out["findings"]


def test_stale_resume_plan_names_the_plan_only_when_not_decent(monkeypatch):
    monkeypatch.setattr(ask_compose, "_static_plan_rel", lambda root, sizing: "docs/plans/p.md")
    monkeypatch.setattr(plan_currency, "assess", lambda root, plan: {"decent": False, "findings": ["x"]})
    assert ask_compose.stale_resume_plan("R", "s.yaml") == "docs/plans/p.md"
    monkeypatch.setattr(plan_currency, "assess", lambda root, plan: {"decent": True, "findings": []})
    assert ask_compose.stale_resume_plan("R", "s.yaml") is None
    monkeypatch.setattr(ask_compose, "_static_plan_rel", lambda root, sizing: None)
    assert ask_compose.stale_resume_plan("R", "s.yaml") is None
