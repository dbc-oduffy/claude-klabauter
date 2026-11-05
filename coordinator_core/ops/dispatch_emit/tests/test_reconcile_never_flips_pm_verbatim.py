"""The ask script's reconcile sites never flip a PM-verbatim row from not_met to met."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW, _compose
from coordinator_core.win_portability import no_console_creationflags

_FAILING = "coordinator_core/tests/test_x.py::test_probe"
_NORMALISE_FLIP = "else { _reconciled.push(i.id); i.status = 'met'; delete i.remaining; }"


def _run_latest_test(ledger, pm_ids) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        f"let _falsifierResult = {json.dumps({'status': 'not_met', 'failing': [_FAILING], 'reason': 'x'})};\n"
        f"let _testResult = {json.dumps({'status': 'pass'})};\n"
        f"let _ledger = {json.dumps(ledger)};\n"
        f"const _pmVerbatimIds = {json.dumps(pm_ids)};\n"
        + ask_compose._criterion_latest_test_reconcile_js()
        + "\nconsole.log(JSON.stringify(_falsifierResult));\n"
    )
    proc = subprocess.run([node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_latest_test_reconcile_refuses_while_a_pm_verbatim_row_is_not_met():
    ledger = {"items": [{"id": "R1", "status": "not_met"}, {"id": "R2", "status": "met"}]}
    assert _run_latest_test(ledger, ["R1"])["status"] == "not_met"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_latest_test_reconcile_still_flips_when_no_pm_verbatim_row_is_open():
    ledger = {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "not_met"}]}
    assert _run_latest_test(ledger, ["R1"])["status"] == "met"
    assert _run_latest_test(None, [])["status"] == "met"


def test_emitted_script_declares_pm_verbatim_ids_from_the_register(monkeypatch):
    monkeypatch.setattr(ask_compose, "_pm_verbatim_row_ids", lambda *_: ["R1", "R2"])
    script = _compose(in_run_commit=True)
    assert 'const _pmVerbatimIds = ["R1", "R2"];' in script


def test_normalise_ledger_keeps_a_pm_verbatim_row_not_met():
    script = _compose(in_run_commit=True)
    assert "else if (_pmVerbatimIds.includes(i.id)) continue; " + _NORMALISE_FLIP in script


def test_reconcile_judged_refuses_a_pm_verbatim_row_reconciled_to_met():
    script = _compose(in_run_commit=True, **_JUDGE_KW)
    assert "!_reconciled.some((id) => _pmVerbatimIds.includes(id))" in script


def test_pm_verbatim_row_ids_selects_only_pm_verbatim_anchors(monkeypatch, tmp_path):
    rows = [
        {"id": "R1", "anchor": "pm_verbatims"},
        {"id": "R2", "anchor": "ask"},
        {"id": "R3", "anchor": "revision@2026-11-04T00:00:00Z"},
    ]
    monkeypatch.setattr(ask_compose, "load_sizing_effective", lambda *_: {"requirement_register": {"rows": rows}})
    assert ask_compose._pm_verbatim_row_ids(str(tmp_path), "state/sizings/x.yaml") == ["R1"]
    assert ask_compose._pm_verbatim_row_ids(str(tmp_path), None) == []
