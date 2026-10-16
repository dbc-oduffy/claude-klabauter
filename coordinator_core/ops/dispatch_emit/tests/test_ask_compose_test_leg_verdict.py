"""The ask script halts when an owed test leg returns no verdict, rather than passing the run to commit."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import PurePath

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import (
    _test_verdict_empty_halt_js,
    _usage_limit_helper_js,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import record_path
from coordinator_core.warp_fire import fire_command

_SIZING = "state/sizings/x.yaml"

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=PurePath(record_path("", "sizings", "x.yaml")).as_posix(),
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_composed_script_halts_on_an_empty_test_verdict(monkeypatch):
    script = _script(monkeypatch)
    halt_at = script.index("halted: \"test_verdict_empty\"")
    assert "await parallel([" in script[:halt_at]
    assert halt_at < script.index("_nextAction = _closeable")
    assert "TEST_GUARD" not in script


_HARNESS = """
const _TEST_GUARD = %s;
let _halted = null, _runId = 'r', _testResult = %s;
if (!_halted && (_TEST_GUARD) && %s
console.log(JSON.stringify(_halted));
"""


def _run(testResult: str, owed: bool) -> object:
    node = shutil.which("node")
    snippet = _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    body = snippet.split("&& (_TEST_GUARD) && ", 1)[1]
    program = _HARNESS % ("true" if owed else "false", testResult, body)
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True).stdout.strip()
    import json

    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "result",
    [
        "null",
        "{ status: 'not_run', sidecar_path: 'a.md' }",
        "{ status: 'pass', sidecar_path: '' }",
        "{ status: 'pass' }",
        "{ status: '', sidecar_path: 'a.md' }",
    ],
)
def test_owed_leg_with_no_verdict_halts(result):
    halted = _run(result, owed=True)
    assert halted and halted["halted"] == "test_verdict_empty"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_real_verdict_and_unowed_leg_do_not_halt():
    assert _run("{ status: 'pass', sidecar_path: 'a.md' }", owed=True) is None
    assert _run("{ status: 'fail', sidecar_path: 'a.md' }", owed=True) is None
    assert _run("null", owed=False) is None


def test_test_verdict_empty_halt_names_the_refire_not_journal_replay():
    js = _test_verdict_empty_halt_js(_SIZING)
    assert "resumeFromRunId" not in js
    assert fire_command(_SIZING) in js.replace('\\"', '"')


def test_usage_limit_halt_still_names_journal_replay():
    assert "resumeFromRunId" in _usage_limit_helper_js()
