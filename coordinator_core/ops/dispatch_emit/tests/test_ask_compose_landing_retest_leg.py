"""The composed ask script's landing leg performs the `targeted-tests-failed` remedy in-run.

A landing refusal carrying `remedy_step` launches the detached retest, polls it and re-lands with
`retest_attempts` raised, bounded by `MAX_IN_RUN_REMEDY_ATTEMPTS`; the emitted helper runs under node
with the agent and the landing stubbed.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import RETEST_ATTEMPTS_PARAM, RETEST_LAUNCHER

RUN_ID = "ask-20261027T000000-aaaaaa"
NODE = shutil.which("node")
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.skipif(NODE is None, reason="node is not on PATH")]
STEP = {"kind": "targeted-tests-failed", "nodes": ["pkg/tests/test_a.py::test_x", "pkg/tests/test_b.py"],
        "attempt": 1, "max_attempts": MAX_IN_RUN_REMEDY_ATTEMPTS, "exhausted": False}

_HARNESS = """
let _halted = null;
const _runId = %(run)s;
const _nextAction = { params: { script_path: 'scratch/warp/x.mjs' } };
const _USAGE_LIMIT_RE = /usage limit/;
function _haltOnUsageLimit(e) { _halted = { halted: 'usage_limit' }; }
%(helper)s
(async () => {
  const replies = %(replies)s; const exits = %(exits)s;
  const log = { prompts: [], lands: [] };
  globalThis.agent = async (prompt) => { log.prompts.push(prompt); return { exit_code: exits.shift() ?? 0 }; };
  const out = await _landRetest(async () => { log.lands.push(_retestAttempts); return replies.shift(); });
  console.log(JSON.stringify({ out, log }));
})();
"""


def _run(replies: list, exits: list) -> dict:
    script = _HARNESS % {
        "run": json.dumps(RUN_ID), "helper": ask_compose._land_retest_js(RUN_ID),
        "replies": json.dumps(replies), "exits": json.dumps(exits),
    }
    done = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_failing_landing_retests_the_named_files_then_relands_with_the_attempt_raised():
    res = _run([{"committed": False, "remedy_step": STEP}, {"committed": True, "sha": "abc"}], [0])
    assert res["out"]["committed"] is True
    assert res["log"]["lands"] == [0, 1]
    (prompt,) = res["log"]["prompts"]
    assert RETEST_LAUNCHER in prompt and "--poll" in prompt
    assert prompt.count(" --tests pkg/tests/test_a.py") == 1 and "--tests pkg/tests/test_b.py" in prompt
    assert "test_x" not in prompt


def test_a_retest_that_never_passes_is_bounded_and_the_spent_landing_is_the_halt():
    spent = {**STEP, "attempt": MAX_IN_RUN_REMEDY_ATTEMPTS + 1, "exhausted": True}
    res = _run(
        [{"committed": False, "remedy_step": STEP}, {"committed": False, "remedy_step": spent}],
        [1] * MAX_IN_RUN_REMEDY_ATTEMPTS,
    )
    assert len(res["log"]["prompts"]) == MAX_IN_RUN_REMEDY_ATTEMPTS
    assert res["log"]["lands"] == [0, MAX_IN_RUN_REMEDY_ATTEMPTS]
    assert res["out"]["remedy_step"]["exhausted"] is True


def test_a_refusal_without_a_remedy_step_or_with_a_spent_one_launches_nothing():
    plain = _run([{"committed": False, "refused": "peer-contested"}], [])
    assert plain["log"] == {"prompts": [], "lands": [0]}
    spent = _run([{"committed": False, "remedy_step": {**STEP, "exhausted": True}}], [])
    assert spent["log"] == {"prompts": [], "lands": [0]}


def test_the_commit_stage_lands_through_the_leg_and_carries_the_attempt_count():
    stage = ask_compose._commit_stage_call(head="h", anchor="a", repo_root="/r", run_id=RUN_ID)
    assert stage.startswith("_landRetest(() => agent(")
    assert f"{RETEST_ATTEMPTS_PARAM}: _retestAttempts" in stage
    assert "remedy_step" in stage
