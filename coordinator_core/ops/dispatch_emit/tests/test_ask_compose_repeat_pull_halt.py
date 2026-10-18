"""compose_ask_script: a re-gate pull on the same blocking set halts without a refire; other cases keep the old halt.

The halt fires only when planBlitz copies the op reply's blocking into pulled[]; that
forwarding lives in the plan-blitz source, outside this suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _compare_line(script: str) -> str:
    start = script.index("if (!_planRel && Array.isArray(_pulledEntry.blocking)")
    return script[start : script.index("\n", start)]


def _halt_clause(script: str) -> str:
    start = script.index("if (_repeatBlocking && !_halted.next_action)")
    return script[start : script.index("\n", start)]


def test_repeat_halt_reason_and_payload_and_no_refire():
    clause = _halt_clause(_script())
    assert "reason: 'plan-blitz pulled twice on the same blocking set'" in clause
    assert "blocking: _repeatBlocking" in clause
    assert "rule_on_blocking_rows" in clause
    assert "refire" not in clause


def test_old_halt_string_and_emission_unchanged():
    script = _script()
    assert "reason: 'plan-blitz reported no ready plan', blitz: _blitz, sizing: _sizingRel, run_id: _runId };" in script
    assert script.index("plan-blitz reported no ready plan") < script.index("if (_repeatBlocking && !_halted.next_action)")


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize(
    "first,second,expect_halt",
    [
        (["a", "b"], ["a", "b"], True),
        (["a", "b"], ["a", "c"], False),
        (["a", "b"], ["b", "a"], False),
        (None, ["a"], False),
        (["a"], None, False),
        ([], [], False),
    ],
)
def test_comparison_runs_over_equal_different_and_absent_sets(first, second, expect_halt):
    line = _compare_line(_script())
    first_obj = {} if first is None else {"blocking": first}
    second_obj = {} if second is None else {"blocking": second}
    js = (
        f"const _pulledEntry = {json.dumps(first_obj)}; const _rePulled = {json.dumps(second_obj)};"
        "let _planRel = null; let _repeatBlocking = null;"
        f"{line}\nconsole.log(JSON.stringify(_repeatBlocking));"
    )
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    assert (json.loads(out) is not None) is expect_halt
