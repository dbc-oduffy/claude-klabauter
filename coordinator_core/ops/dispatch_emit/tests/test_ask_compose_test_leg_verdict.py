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


def _script(monkeypatch, **extra) -> str:
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
        **extra,
    )


def test_composed_script_halts_on_an_empty_test_verdict(monkeypatch):
    script = _script(monkeypatch)
    halt_at = script.index("halted: \"test_verdict_empty\"")
    assert "await Promise.all([!_halted ? _runVerify()" in script[:halt_at]
    assert halt_at < script.index("_nextAction = _closeable")
    assert "TEST_GUARD" not in script


# The composed script declares `_testRan` once, ahead of the halt, beside the owed-condition.
_TEST_RAN_DECL = f"const {ask_compose._TEST_RAN_FN} = () => {ask_compose._TEST_RAN_JS};"

_HARNESS = """
%s
const _TEST_GUARD = %s;
let _halted = null, _runId = 'r', _testResult = %s;
if (!_halted && (_TEST_GUARD) && %s
console.log(JSON.stringify(_halted));
"""


def _run(testResult: str, owed: bool) -> object:
    node = shutil.which("node")
    snippet = _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    helpers, rest = snippet.split("    if (!_halted", 1)
    body = rest.split("&& (_TEST_GUARD) && ", 1)[1]
    program = _HARNESS % (_TEST_RAN_DECL + "\n" + helpers, "true" if owed else "false", testResult, body)
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
    assert halted["halted"] == "test_infra_failure"
    assert f"ran zero tests (import-error): {_DIRTY} ({named})" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_foreign_dependency_halt_names_the_source_to_land_not_a_bare_refire():
    extra = f", import_error: {{ path: '{_DIRTY}', run_id: null, symbol: 'select', source_path: '{_DIRTY}' }}"
    halted = _run(_ZERO % ("uncommitted-foreign-dependency", extra), owed=True)
    assert halted["next_action"].startswith(f"Land the uncommitted {_DIRTY} first, then refire: ")
    plain = _run(_ZERO % ("collection-failure", ""), owed=True)
    assert plain["next_action"].startswith("Refire to re-run the test leg: ")


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_collection_failure_halt_detail_names_no_path():
    halted = _run(_ZERO % ("collection-failure", ""), owed=True)
    assert "ran zero tests (collection-failure);" in halted["detail"]
    assert "(run " not in halted["detail"]


_REVERIFY_PROGRAM = """
%s
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
        [shutil.which("node"), "-"], input=_REVERIFY_PROGRAM % (_TEST_RAN_DECL, decl, body), capture_output=True, text=True,
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


_AT_BASE_PROGRAM = """
const _TEST_GUARD = true;
let _halted = null, _runId = 'r', _testResult = %s;
let _reviewReverify = %s;
%s
console.log(JSON.stringify(_halted));
"""


def _run_at_base(test_result: str, reverify: str) -> dict:
    import json

    body = _TEST_RAN_DECL + chr(10) + _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    out = subprocess.run(
        [shutil.which("node"), "-"], input=_AT_BASE_PROGRAM % (test_result, reverify, body),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "test_result, reverify",
    [
        ("null", "{ status_after: 'fail', failing: ['a::t1'], owned_red: ['a::t1'] }"),
        ("null", "{ status_after: 'fail', failing: ['a::t1'], peer_dirt: ['a::t1'] }"),
        ("{ status: 'error', sidecar_path: 'a.md', pre_existing: ['a::t1'] }",
         "{ status_after: 'fail', failing: ['a::t1'] }"),
        ("{ status: 'error', sidecar_path: 'a.md', failing_at_base: ['a::t1'] }",
         "{ status_after: 'fail', failing: ['a::t1'] }"),
    ],
)
def test_id_measured_red_at_base_is_never_listed_as_not_red_at_the_run_base(test_result, reverify):
    halted = _run_at_base(test_result, reverify)
    assert halted["halted"] == "test_verdict_empty"
    assert halted["failing"] == []
    assert "not red at the run base" not in halted["detail"]
    assert "all pre-existing" in halted["detail"] and "a::t1" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_id_with_no_at_base_measurement_stays_listed_as_not_red_at_the_run_base():
    halted = _run_at_base(
        "{ status: 'error', sidecar_path: 'a.md', failing_at_base: ['a::t1'] }",
        "{ status_after: 'fail', failing: ['a::t1', 'b::t2'], owned_red: ['c::t3'] }",
    )
    assert halted["failing"] == ["b::t2"]
    assert "not red at the run base: b::t2" in halted["detail"]


_CO_TENANT = "co_tenant: { owner: 'run-7', module: 'coordinator_core/ops/run_order.py'%s }"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_co_tenant_wait_halts_co_tenant_wait_naming_owner_and_module():
    halted = _run_at_base(
        "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: 'co-tenant-wait', "
        + _CO_TENANT % "" + " }",
        "null",
    )
    assert halted["halted"] == "co_tenant_wait" != "test_infra_failure"
    assert "run-7" in halted["detail"] and "coordinator_core/ops/run_order.py" in halted["detail"]
    assert halted["next_action"] == "the engine refires run-7 first, then this run (stranded-wait sweep)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_co_tenant_on_halted_run_halts_that_kind_naming_the_owner_refire():
    halted = _run_at_base(
        "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, "
        "zero_run_cause: 'co_tenant_dependency_on_halted_run', "
        + _CO_TENANT % ", owner_refire: 'REFIRE-RUN-7'" + " }",
        "null",
    )
    assert halted["halted"] == "co_tenant_dependency_on_halted_run"
    assert "REFIRE-RUN-7" in halted["next_action"] and "run-7" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_unrelated_zero_run_cause_still_halts_the_empty_verdict_kind():
    halted = _run_at_base(
        "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: 'collection-failure' }", "null"
    )
    # A zero-run no co-tenant caused is a test-infra failure; only the co-tenant causes keep their own halt kinds.
    assert halted["halted"] == "test_infra_failure"
    assert halted["next_action"].startswith("Refire to re-run the test leg")


_OVER_BUDGET = (
    "{ status: 'error', leg_type: 'test_infra', sidecar_path: 'a.md', summary: 'timeout: 540s', "
    "over_budget: true, elapsed_s: 541.2, budget_s: 540, tests_run: 155, tests_failed: 0, "
    "slow_targets: [{ file: 'test_map_gate_v1_offline.py', seconds: 374 }], "
    "unrun_targets: ['u1.py', 'u2.py', 'u3.py', 'u4.py'] }"
)


def test_test_result_schema_keeps_every_field_the_over_budget_halt_reads():
    props = ask_compose._test_result_schema()["properties"]
    for key in ("over_budget", "elapsed_s", "budget_s", "slow_targets", "unrun_targets", "summary",
                "failing_detail", "error", "stderr_tail", "tests_run", "tests_failed"):
        assert key in props


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_leg_budget_timeout_halts_test_leg_over_budget_naming_its_measurements():
    halted = _run_at_base(_OVER_BUDGET, "null")
    assert halted["halted"] == "test_leg_over_budget"
    detail = halted["detail"]
    for named in ("timeout: 540s", "elapsed 541.2s of 540s budget", "test_map_gate_v1_offline.py (374s)", "u1.py, u2.py, u3.py, u4.py",
                  "tests_run 155 / tests_failed 0"):
        assert named in detail
    assert "errored without a failing id" not in detail
    assert halted["next_action"].startswith("Refire to re-run the test leg")


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "result, carried",
    [
        ("{ status: 'error', sidecar_path: 'a.md', summary: 'test leg spawn failed: OSError: boom' }", "OSError: boom"),
        ("{ status: 'error', sidecar_path: 'a.md', over_budget: false, "
         "failing_detail: [{ node_id: 'pytest', message: 'pytest exit 2 with no failing id' }] }", "pytest exit 2"),
        ("{ status: 'error', sidecar_path: 'a.md', error: 'Traceback: KeyError x' }", "KeyError x"),
        ("{ status: 'errored', sidecar_path: 'a.md', stderr_tail: ['line one', 'last stderr line'] }", "last stderr line"),
    ],
)
def test_other_errored_leg_carries_its_cause_and_stays_test_verdict_empty(result, carried):
    halted = _run_at_base(result, "null")
    assert halted["halted"] == "test_verdict_empty"
    assert carried in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_errored_leg_with_no_cause_fields_still_names_the_absence():
    halted = _run_at_base("{ status: 'error', sidecar_path: 'a.md' }", "null")
    assert halted["halted"] == "test_verdict_empty"
    assert "carried no summary, failing_detail message, error or stderr tail" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize("result", [
    "{ status: 'error', sidecar_path: 'a.md', over_budget: false, tests_run: 5, summary: 's' }",
    "{ status: 'pass', sidecar_path: 'a.md', over_budget: true, tests_run: 5 }",
])
def test_only_an_errored_over_budget_verdict_halts_test_leg_over_budget(result):
    halted = _run_at_base(result, "null")
    assert halted is None or halted["halted"] != "test_leg_over_budget"


def test_export_test_instruction_tells_the_leg_to_report_the_co_tenant_line():
    text = ask_compose._test_instruction_js("a" * 40, None, "EXPORT-CMD")
    assert "co_tenant_dependency: {...}" in text and "`co_tenant`" in text


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


def test_composed_script_never_refires_a_zero_test_infra_verdict(monkeypatch):
    script = _script(monkeypatch)
    for gone in ("_tiN", "refiring the verify leg"):
        assert gone not in script
    assert 'halted: "test_infra_failure"' in script
    assert "TEST_GUARD" not in script
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov, halted_record_eviction as hre

    assert ask_compose.TEST_INFRA_CAUSE == cov.ZERO_RUN_TEST_INFRA == hre.TEST_INFRA_HALT
    assert not hasattr(ask_compose, "TEST_INFRA_REFIRE_LIMIT")


_SESSION = (
    ", infra_session: { argv: ['py', '-m', 'pytest', 'tests/semantic/test_symbol_name_index.py'], "
    "cwd: 'the-tree', interpreter: 'the-python', exit_code: 4 }, "
    "first_setup_error: 'ImportError while loading tests/conftest.py: NameError: name contextmanager is not defined'"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_zero_test_infra_verdict_halts_test_infra_failure_naming_its_cause_and_routes_to_a_fix():
    halted = _run_at_base(_ZERO % ("test_infra_failure", _SESSION), "null")
    assert halted["halted"] == "test_infra_failure"
    for named in ("ran zero tests (test_infra_failure)", "NameError: name contextmanager is not defined",
                  "tests/semantic/test_symbol_name_index.py", "cwd the-tree", "interpreter the-python", "exit_code 4"):
        assert named in halted["detail"]
    assert halted["next_action"].startswith("Fix the cause named in detail first")
    assert fire_command(_SIZING) in halted["next_action"].replace('\\"', '"')


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_zero_test_infra_verdict_without_a_session_halts_naming_the_absence():
    halted = _run_at_base(_ZERO % ("test_infra_failure", ""), "null")
    assert halted["halted"] == "test_infra_failure"
    assert "carried no pytest session" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize("held", [
    "{ status: 'error', sidecar_path: 'a.md', tests_run: 3, zero_run_cause: 'test_infra_failure' }",
    "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: 'co-tenant-wait' }",
])
def test_only_a_zero_test_infra_verdict_halts_test_infra_failure(held):
    halted = _run_at_base(held, "null")
    assert halted is None or halted["halted"] != "test_infra_failure"


_LEDGER_HARNESS = """
let _ledger = %s; const _reqText = %s;
%s
%s
let _commit = %s, _blkIds = %s, _emptyVerdict = false, _missingIds = [];
const _halted = %s;
console.log(JSON.stringify({ halted_by: _halted }));
"""

_UNMET_ITEMS = (
    "{ items: [{ id: 'R1', status: 'not_met', root_read: ['coordinator_core/x.py'], "
    "evidence: 'x.py has no halt cause', remaining: ['halt detail still bare'] }, { id: 'R2', status: 'met' }] }"
)
_REQ_TEXT = "{ R1: 'A test-leg error must carry its stderr tail', N1: 'Widen to the ledger halt' }"
_COMMIT_FIELDS = ask_compose._COMMIT_LEDGER_FIELDS_JS + "reason: " + ask_compose._COMMIT_HALT_REASON_WITH_LEDGER_JS


def _node_halted_by(ledger: str, req_text: str, commit: str, blk_ids: str, halted: str) -> dict:
    import json

    out = subprocess.run(
        [shutil.which("node"), "-"],
        input=_LEDGER_HARNESS % (ledger, req_text, ask_compose.UNMET_ROWS_DETAIL_JS,
                                 ask_compose.COMMIT_LEDGER_DETAIL_JS, commit, blk_ids, halted),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)["halted_by"]


def _ledger_halt(kind: str, ledger: str = _UNMET_ITEMS, req_text: str = _REQ_TEXT) -> dict:
    commit = "{ refused: 'ledger-unmet', error: 'the requirements are not met: R1: not met' }"
    halted = {
        "commit": "{ halted: 'commit', " + _COMMIT_FIELDS + " }",
        "ledger-check": "{ halted: 'ledger-check', " + ask_compose.LEDGER_CHECK_DETAIL_JS + " }",
    }[kind]
    return _node_halted_by(ledger, req_text, commit, "['R1']", halted)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize("kind", ["commit", "ledger-check"])
def test_an_unmet_ledger_row_halts_naming_requirement_evidence_and_gap(kind):
    from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message

    halted_by = _ledger_halt(kind)
    for field in ("detail", "reason") if kind == "commit" else ("detail",):
        text = halted_by[field]
        assert "R1" in text
        assert "A test-leg error must carry its stderr tail" in text
        assert "root_read coordinator_core/x.py" in text and "x.py has no halt cause" in text
        assert "halt detail still bare" in text
        assert "R2" not in text
    assert "A test-leg error must carry its stderr tail" in halted_record_message("r", halted_by)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_bare_not_met_detail_is_refused():
    detail = _ledger_halt("commit", ledger="{ items: [] }", req_text="{}")["detail"]
    assert detail != "R1: not met"
    assert "requirement text not in the ledger view" in detail
    assert "evidence checked: none recorded by the verify leg" in detail
    assert "remaining gap: the verdict named no remaining line (not met)" in detail


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_the_verify_items_own_requirement_text_serves_a_row_the_ledger_view_lacked():
    ledger = "{ items: [{ id: 'R1', status: 'not_met', requirement: 'late row first line\\nsecond', remaining: ['g'] }] }"
    assert "late row first line" in _ledger_halt("ledger-check", ledger=ledger, req_text="{}")["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_a_non_ledger_commit_refusal_keeps_its_own_reason_and_no_detail():
    halted_by = _node_halted_by(
        "null", "{}", "{ refused: 'blob-drift', error: 'x moved' }", "[]", "{ halted: 'commit', " + _COMMIT_FIELDS + " }"
    )
    assert halted_by["reason"] == "blob-drift: x moved" and halted_by["detail"] is None


def test_requirement_first_lines_reads_register_rows_and_revise_notes():
    view = (
        "## R1 (relayed)\n\n> first line of R1\n> second\n\n## Revise notes\n\n"
        "run-x-20261122T093412-72f584 N1 at 2026-11-22T10:11:20Z:\n\n> Widen to the commit-leg halt\n"
    )
    assert ask_compose.requirement_first_lines(view) == {"R1": "first line of R1", "N1": "Widen to the commit-leg halt"}


def test_composed_script_wires_the_unmet_row_detail_into_both_halts(monkeypatch):
    # The commit halt exists only in a script that lands its own terminal commit.
    script = _script(monkeypatch, in_run_commit=True).replace("\r", "")
    assert ask_compose.LEDGER_CHECK_DETAIL_JS in script
    assert _COMMIT_FIELDS in script
    assert script.index("const _unmetRowsDetail") < script.index("halted: 'ledger-check'")
    commit_at = script.index("_halted = { halted: 'commit', refused:")
    assert commit_at < script.index(_COMMIT_FIELDS) < script.index("run_id: _runId };", commit_at)
    ledger_at = script.index("_halted = { halted: 'ledger-check', ")
    assert ledger_at < script.index(ask_compose.LEDGER_CHECK_DETAIL_JS, ledger_at)
    assert "halted_by: _halted" in script[commit_at:]
