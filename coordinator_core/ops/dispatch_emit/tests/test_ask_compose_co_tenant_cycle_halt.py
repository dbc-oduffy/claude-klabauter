"""The ask script's empty-verdict arm halts a `co_tenant_cycle` zero-run cause with the joint-land call."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, co_tenant_cycle, co_tenant_owner
from coordinator_core.ops.dispatch_emit.ask_compose import CO_TENANT_CYCLE_CAUSE, _test_verdict_empty_halt_js
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIZING = "state/sizings/x.yaml"
A = "ask-20261121T100000-aaaaaa"
B = "ask-20261121T101000-bbbbbb"

_PROGRAM = """
const _TEST_GUARD = true;
let _halted = null, _runId = %s, _testResult = %s;
let _reviewReverify = null;
%s
console.log(JSON.stringify(_halted));
"""


def _run(test_result: str) -> dict:
    # The composed script declares `_testRan` once, ahead of the halt; the snippet alone does not.
    decl = f"const {ask_compose._TEST_RAN_FN} = () => {ask_compose._TEST_RAN_JS};\n"
    body = decl + _test_verdict_empty_halt_js(_SIZING).replace("TEST_GUARD", "_TEST_GUARD")
    out = subprocess.run(
        [shutil.which("node"), "-"], input=_PROGRAM % (json.dumps(A), test_result, body),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    return json.loads(out)


def _verdict(cause: str, co_tenant: str) -> str:
    return f"{{ status: 'error', sidecar_path: 'a.md', tests_run: 0, zero_run_cause: '{cause}', co_tenant: {co_tenant} }}"


def test_cause_mirrors_the_cycle_module() -> None:
    assert CO_TENANT_CYCLE_CAUSE == co_tenant_cycle.CAUSE_CYCLE


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_cycle_verdict_halts_co_tenant_cycle_with_the_joint_land_call() -> None:
    remedy = co_tenant_cycle.joint_remedy((A, B))
    co_tenant = json.dumps({"owner": B, "module": "coordinator_core.install", "cycle": [A, B], "remedy": remedy})
    halted = _run(_verdict("co_tenant_cycle", co_tenant))
    assert halted["halted"] == "co_tenant_cycle" and halted["halted"] not in ("test_infra_failure", "test_verdict_empty")
    assert halted["next_action"] == "Joint-land the cycle: " + remedy
    assert A in halted["detail"] and B in halted["detail"] and "coordinator_core.install" in halted["detail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_cycle_verdict_without_a_remedy_builds_the_call_from_the_cycle() -> None:
    halted = _run(_verdict("co_tenant_cycle", json.dumps({"owner": B, "cycle": [A, B]})))
    assert halted["halted"] == "co_tenant_cycle"
    assert halted["next_action"] == "Joint-land the cycle: coordinator-invoke dispatch.land_run " + json.dumps({"joint": [A, B]}, separators=(",", ":"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize(
    ("cause", "kind"), [("co-tenant-wait", "co_tenant_wait"), ("co_tenant_dependency", "co_tenant_dependency")]
)
def test_wait_and_unowned_causes_do_not_halt_as_a_cycle(cause: str, kind: str) -> None:
    halted = _run(_verdict(cause, json.dumps({"owner": B, "module": "m"})))
    assert halted["halted"] == kind
    assert "Joint-land" not in halted["next_action"]


def test_unowned_cause_mirrors_the_owner_module() -> None:
    assert ask_compose.CO_TENANT_UNOWNED_CAUSE == co_tenant_owner.CAUSE_TYPED


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_ownerless_dependency_verdict_halts_with_the_land_or_drop_next_action() -> None:
    co_tenant = json.dumps({"owner": None, "module": "coordinator_core.ops.run_order", "symbol": "run_order", "importer": "a.py"})
    halted = _run(_verdict("co_tenant_dependency", co_tenant))
    assert halted["halted"] == "co_tenant_dependency" and halted["halted"] not in ("test_infra_failure", "test_verdict_empty")
    assert halted["next_action"].startswith(
        "Land the needed symbol or drop the import: run_order in coordinator_core.ops.run_order, imported by a.py"
    )
    assert "Refire the owner first" not in halted["next_action"] and "Refire the owner first" not in halted["detail"]
    assert "null" not in halted["detail"] and "undefined" not in halted["next_action"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_cycle_verdict_errored_over_budget_still_halts_the_cycle() -> None:
    halted = _run(
        "{ status: 'error', sidecar_path: 'a.md', tests_run: 0, over_budget: true, zero_run_cause: 'co_tenant_cycle', "
        "co_tenant: { owner: 'b', cycle: ['a', 'b'], remedy: 'R' } }"
    )
    assert halted["halted"] == "co_tenant_cycle" and halted["next_action"] == "Joint-land the cycle: R"
