"""The ask script halts when an owed test leg returns no verdict, rather than passing the run to commit."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import PurePath

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import (
    STALL_ONLY_COERCE_JS,
    _test_verdict_empty_halt_js,
    _usage_limit_helper_js,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import record_path
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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
    assert "await Promise.all([!_halted ? _runVerify()" in script[:halt_at]
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
    out = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()
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


_ZERO = "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: '%s'%s }"
_DIRTY = "coordinator_core/ops/reverify_scope.py"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "run_id, named",
    [("'run-9'", "run run-9"), ("null", "run unowned")],
)
def test_import_error_halt_detail_names_the_dirty_path_and_owning_run(run_id, named):
    extra = f", import_error: {{ path: '{_DIRTY}', run_id: {run_id} }}"
    halted = _run(_ZERO % ("import-error", extra), owed=True)
    assert halted["halted"] == "test_verdict_empty"
    assert f"ran zero tests (import-error): {_DIRTY} ({named})" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_collection_failure_halt_detail_names_no_path():
    halted = _run(_ZERO % ("collection-failure", ""), owed=True)
    assert "ran zero tests (collection-failure);" in halted["detail"]
    assert "(run " not in halted["detail"]


_REVERIFY_PROGRAM = """
const _TEST_GUARD = true;
let _halted = null, _runId = 'r', _testResult = null;
%s
%s
console.log(JSON.stringify(_halted));
"""


def _run_with_reverify(reverify: str | None) -> object:
    import json

    decl = "" if reverify is None else f"let _reviewReverify = {reverify};"
    body = _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    out = subprocess.run(
        [shutil.which("node"), "-"], input=_REVERIFY_PROGRAM % (decl, body), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "reverify",
    [
        "{ status_after: 'pass', failing: [] }",
        "{ status_after: 'pass' }",
        "{ status_after: 'pass', failing: ['a::t1'], pre_existing: ['a::t1'] }",
        "{ status_after: 'pass', failing: ['a::t1'], kept: ['a::t1'] }",
    ],
)
def test_passing_reverify_stands_in_for_an_empty_test_verdict(reverify):
    assert _run_with_reverify(reverify) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "reverify",
    [
        None,
        "null",
        "{ status_after: 'fail', failing: ['a::t1'] }",
        "{ status_after: 'fail', failing: [] }",
        "{ status_after: null, failing: [] }",
        "{ status_after: 'indeterminate' }",
        "{ status_after: 'pass', failing: ['c::t3'], pre_existing: ['a::t1'] }",
    ],
)
def test_failed_or_absent_reverify_still_halts_on_an_empty_test_verdict(reverify):
    halted = _run_with_reverify(reverify)
    assert halted and halted["halted"] == "test_verdict_empty"


def test_test_verdict_empty_halt_names_the_refire_not_journal_replay():
    js = _test_verdict_empty_halt_js(_SIZING)
    assert "resumeFromRunId" not in js
    assert fire_command(_SIZING) in js.replace('\\"', '"')


def test_ledger_judge_rules_a_stalled_leg_inconclusive_never_not_met():
    rules = ask_compose._LEDGER_ITEMS_RULES
    flat = rules.replace("\n", " ")
    assert "stalled or timed out" in flat
    assert "is inconclusive: judge the item from the change itself" in flat
    assert "set stall_only true on the item" in flat
    assert flat.index("is inconclusive") < flat.index("otherwise 'not_met'")
    assert "zero_run_cause) proves nothing" in flat and "never stall_only" in flat


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_stall_only_not_met_is_coerced_inconclusive_and_other_items_stand():
    import json

    items = [
        {"id": "a", "status": "not_met", "stall_only": True, "remaining": ["verify leg"]},
        {"id": "b", "status": "not_met", "remaining": ["real gap"]},
        {"id": "c", "status": "not_met", "stall_only": False, "remaining": ["real gap"]},
        {"id": "d", "status": "met", "stall_only": True},
    ]
    program = f"const _ledger = {{ items: {json.dumps(items)} }};\n{STALL_ONLY_COERCE_JS}\nconsole.log(JSON.stringify(_ledger.items));"
    out = subprocess.run(
        [shutil.which("node"), "-"], input=program, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    got = {i["id"]: i for i in json.loads(out)}
    assert got["a"]["status"] == "inconclusive" and "remaining" not in got["a"]
    assert [got[k]["status"] for k in "bcd"] == ["not_met", "not_met", "met"]


def test_composed_ledger_normaliser_runs_the_stall_coercion(monkeypatch):
    script = _script(monkeypatch)
    assert STALL_ONLY_COERCE_JS in script
    assert "status === 'drifted' || status === 'inconclusive'" in script


def test_usage_limit_halt_still_names_journal_replay():
    assert "resumeFromRunId" in _usage_limit_helper_js()
