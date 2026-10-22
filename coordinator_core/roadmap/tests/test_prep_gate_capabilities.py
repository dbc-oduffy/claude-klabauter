"""coordinator_core/roadmap/tests/test_prep_gate_capabilities.py — the CAPABILITIES class.

An absent `capabilities` key fails the bar; `capabilities: []` and a non-empty
list pass. Drives `gate_plan` over plans built in `tmp_path`.
"""

from __future__ import annotations

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate import (
    _CLEAN_FM,
    _CLEAN_SPINE,
    _write_plan,
)


def _gate(tmp_path, frontmatter):
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    plan = _write_plan(tmp_path, frontmatter=frontmatter, spine=_CLEAN_SPINE)
    return pg.gate_plan(tmp_path, plan)


def _fm_without_capabilities():
    return _CLEAN_FM.replace("capabilities: []\n", "")


def test_keyless_plan_is_not_prepped(tmp_path):
    report = _gate(tmp_path, _fm_without_capabilities())
    assert report["verdict"] == pg.NOT_PREPPED
    cls = report["classes"]["CAPABILITIES"]
    assert cls["status"] == "DEFECT"
    assert cls["kind"] == pg.CAPABILITIES_UNDECLARED_KIND == "capabilities-undeclared"
    assert "CAPABILITIES" in report["message"]
    assert "capabilities: []" in report["message"]
    assert "mise-prep-upgrade" in report["message"]


def test_empty_list_declares_none_and_passes(tmp_path):
    report = _gate(tmp_path, _CLEAN_FM)
    assert report["classes"]["CAPABILITIES"]["status"] == "PASS"
    assert report["verdict"] == pg.PREPPED


def test_non_empty_list_passes(tmp_path):
    fm = _fm_without_capabilities() + (
        "capabilities:\n"
        "  - name: thing\n"
        "    role: operator\n"
        "    click_path: menu > thing\n"
        "    ui_consumer: C1\n"
    )
    report = _gate(tmp_path, fm)
    assert report["classes"]["CAPABILITIES"]["status"] == "PASS"
