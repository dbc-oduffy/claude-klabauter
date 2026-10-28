"""A falsifier not_met naming failing tests is superseded by a green later _testResult."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.win_portability import no_console_creationflags

_NODE = "coordinator_core/tests/test_x.py::test_probe"


def _run(falsifier, test_result) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    decl = "let _testResult = null;\n" if test_result is ... else f"let _testResult = {json.dumps(test_result)};\n"
    source = (
        f"let _falsifierResult = {json.dumps(falsifier)};\n"
        + decl
        + ask_compose._criterion_latest_test_reconcile_js()
        + "\nconst _criterionOpen = !!(_falsifierResult && _falsifierResult.status === 'not_met');\n"
        "console.log(JSON.stringify({ falsifier: _falsifierResult, criterionOpen: _criterionOpen }));\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _falsifier(**kw) -> dict:
    base = {"status": "not_met", "failing": [_NODE], "reason": "76 scoped tests still fail"}
    base.update(kw)
    return base


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("status", ["pass", "pass-with-skips"])
def test_later_green_test_result_makes_the_verdict_met(status):
    out = _run(_falsifier(), {"status": status, "sidecar_path": "t.md"})

    assert out["falsifier"]["status"] == "met"
    assert out["falsifier"]["reconciled_from"] == "not_met"
    assert out["criterionOpen"] is False


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("test_result", [{"status": "fail"}, {"status": "error"}, None])
def test_red_or_absent_test_result_keeps_not_met(test_result):
    out = _run(_falsifier(), test_result)

    assert out["falsifier"]["status"] == "not_met"
    assert out["criterionOpen"] is True


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_undeclared_test_result_keeps_not_met():
    assert _run(_falsifier(), ...)["criterionOpen"] is True


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_not_met_without_failing_list_or_not_wired_up_stays_open():
    green = {"status": "pass"}

    assert _run({"status": "not_met"}, green)["criterionOpen"] is True
    assert _run(_falsifier(reason="not wired up: x"), green)["criterionOpen"] is True
