"""The empty-test-verdict halt names failing minus pre_existing from a re-verify that reverted nothing."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import _test_verdict_empty_halt_js
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PROGRAM = """
const _TEST_GUARD = true;
let _halted = null, _runId = 'r', _testResult = null;
%s
%s
console.log(JSON.stringify(_halted));
"""


def _run(reverify: str | None) -> dict:
    decl = "" if reverify is None else f"let _reviewReverify = {reverify};"
    body = _test_verdict_empty_halt_js("state/sizings/x.yaml").replace("TEST_GUARD", "_TEST_GUARD")
    out = subprocess.run(
        [shutil.which("node"), "-"], input=_PROGRAM % (decl, body), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_halt_names_the_foreign_red_not_the_pre_existing_ones():
    halted = _run(
        "{ status_after: null, failing: ['a::t1', 'b::t2', 'c::t3'], pre_existing: ['a::t1', 'b::t2'], reverted: [] }"
    )
    assert halted["halted"] == "test_verdict_empty"
    assert halted["failing"] == ["c::t3"]
    assert halted["pre_existing"] == ["a::t1", "b::t2"]
    assert "c::t3" in halted["detail"]
    assert "a::t1" not in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_halt_with_only_pre_existing_reds_names_none_as_this_runs():
    halted = _run("{ status_after: null, failing: ['a::t1'], pre_existing: ['a::t1'], reverted: [] }")
    assert halted["halted"] == "test_verdict_empty"
    assert halted["failing"] == []
    assert "none this run's" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_halt_without_a_reverify_record_still_halts_with_no_set():
    halted = _run(None)
    assert halted["halted"] == "test_verdict_empty"
    assert halted["failing"] == [] and halted["pre_existing"] == []
    assert "re-verify reds" not in halted["detail"]
