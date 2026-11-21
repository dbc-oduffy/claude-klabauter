"""A UE-automation-only run stages and composes with its test criterion owed to the EM; a Python run with zero tests still halts."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import shared_decls, write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node absent"),
]

_PLAN_REL = "docs/plans/2026-11-19-fixture.md"
_UE_ROW = """- id: E1
  title: run the UE automation tests
  change_kind: code-edit
  performer: em
  test_criterion:
    kind: ue-automation
    tests:
      - Project.Foo.Spawns
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
"""


def _plan(writes: list[str], extra_rows: str = "") -> str:
    listed = "\n".join(f"    - {w}" for w in writes)
    return (
        "---\ndeliverable_id: dlv-fixture-abc123\n---\n# Fixture\n\n## Problem\n\nOne code row.\n\n"
        "## Tasks\n\n```yaml plan-tasks\n"
        f"- id: C1\n  title: code\n  change_kind: code-edit\n  surface: {writes[0]}\n  writes:\n{listed}\n"
        f"{extra_rows}```\n"
    )


def _staged(tmp_path, plan_text: str) -> StageManifest:
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _PLAN_REL).write_text(plan_text, encoding="utf-8", newline="\n")
    write_run_scripts(tmp_path, "r1")
    return stage(tmp_path, run_id="r1", plan_rel=_PLAN_REL)


def _line(script: str, anchor: str) -> str:
    """The composed script's one line holding `anchor`, after the script's shared helper declarations."""
    start = script.index(anchor)
    return shared_decls(script) + "\n" + script[script.rfind("\n", 0, start) + 1 : script.find("\n", start)]


def _run(segment: str, manifest: dict, **state: str) -> dict:
    """Evaluate one composed script line under node over the staged `manifest`; returns the resulting `_halted` and `_testResult`."""
    decls = {
        "_halted": "null", "_reviewReused": "false", "_codeReviewOwed": "true", "_testResult": "null",
        "_reviewReverify": "{ status_after: null, status_before: null }", "_gate": "{ arm: 'm' }",
        "_xsTail": "false", "_reviewApplied": "0", "_runId": "'r1'", "_planRel": "'docs/plans/p.md'",
        "_refireCmd": "() => 'refire'", **state,
    }
    head = "".join(f"let {k} = {v};\n" for k, v in decls.items())
    program = (
        head + f"const _manifest = {json.dumps(manifest)};\n{segment}\n"
        "console.log(JSON.stringify({ halted: _halted, testResult: _testResult }));\n"
    )
    proc = subprocess.run(
        [shutil.which("node"), "-"], input=program, capture_output=True, text=True, timeout=60,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def script() -> str:
    return _compose()


def test_arm_a_cpp_run_with_an_em_performed_ue_criterion_stages_it_owed_and_composes_no_halt(tmp_path, script):
    manifest = _staged(tmp_path, _plan(["Plugin/Source/Foo.cpp", "Plugin/Source/Foo.h"], _UE_ROW))
    assert manifest.em_owed_test_criterion == {"row": "E1", "kind": "ue-automation", "tests": ["Project.Foo.Spawns"]}
    assert manifest.test_targets == ()
    staged = json.loads((tmp_path / RECORD_DIR_ROOT / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert staged["em_owed_test_criterion"]["row"] == "E1"

    not_run = _run(_line(script, 'halted: "tests_not_run"'), staged)
    assert not_run["halted"] is None
    empty = _run(_line(script, 'halted: "test_verdict_empty"'), staged)
    assert empty["halted"] is None

    owed = _run(_line(script, "owed_to: 'em'"), staged)["testResult"]
    assert owed["status"] == "degraded" and owed["owed_to"] == "em" and owed["row"] == "E1"
    assert '"verb":"em-discharge"' in owed["summary"] and '"id":"E1"' in owed["summary"]
    assert "docs/plans/p.md" in owed["summary"] and "ue_report" in owed["summary"]


def test_arm_a_halts_again_once_the_owed_criterion_is_removed_from_the_manifest(tmp_path, script):
    staged = _staged(tmp_path, _plan(["Plugin/Source/Foo.cpp", "Plugin/Source/Foo.h"], _UE_ROW)).to_json()
    staged["em_owed_test_criterion"] = None
    halted = _run(_line(script, 'halted: "tests_not_run"'), staged)["halted"]
    assert halted and halted["halted"] == "tests_not_run"
    assert _run(_line(script, "owed_to: 'em'"), staged)["testResult"] is None


def test_the_verdict_empty_guard_never_exempts_a_run_with_a_runnable_pytest_target(tmp_path, script):
    staged = _staged(tmp_path, _plan(["Plugin/Source/Foo.cpp", "Plugin/Source/Foo.h"], _UE_ROW)).to_json()
    line = _line(script, 'halted: "test_verdict_empty"')
    assert _run(line, staged)["halted"] is None  # no pytest target: the guard never arms

    staged["test_targets"] = ["pkg/tests/test_x.py"]
    for owed in (staged["em_owed_test_criterion"], None):
        staged["em_owed_test_criterion"] = owed
        halted = _run(line, staged)["halted"]
        assert halted and halted["halted"] == "test_verdict_empty"


def test_arm_b_python_write_is_never_exempt_and_keeps_the_tests_not_run_halt(tmp_path, script):
    manifest = _staged(tmp_path, _plan(["pkg/a.py"], _UE_ROW))
    assert manifest.em_owed_test_criterion is None and manifest.test_targets == ()
    staged = manifest.to_json()
    not_run, owed = _line(script, 'halted: "tests_not_run"'), _line(script, "owed_to: 'em'")

    # A .py write the locator maps to no test is degraded by its own record, as today.
    assert staged["no_test_target"]["unmapped_paths"] == ["pkg/a.py"]
    assert _run(not_run, staged)["halted"] is None
    assert _run(owed, staged)["testResult"] is None

    # Without that record the zero-test run halts, and the UE criterion on the plan does not exempt it.
    staged["no_test_target"] = None
    halted = _run(not_run, staged)["halted"]
    assert halted and halted["halted"] == "tests_not_run"
    assert _run(owed, staged)["testResult"] is None


def test_arm_c_cpp_only_plan_without_the_declaration_is_not_owed_and_behaves_as_before(tmp_path, script):
    manifest = _staged(tmp_path, _plan(["Plugin/Source/Foo.cpp", "Plugin/Source/Foo.h"]))
    assert manifest.em_owed_test_criterion is None and manifest.test_targets == ()
    staged = manifest.to_json()
    assert staged["em_owed_test_criterion"] is None

    assert _run(_line(script, "owed_to: 'em'"), staged)["testResult"] is None
    # The staged no_test_target record alone decides the halt, exactly as without the owed field.
    assert (_run(_line(script, 'halted: "tests_not_run"'), staged)["halted"] is None) == bool(staged["no_test_target"])
    staged["no_test_target"] = None
    halted = _run(_line(script, 'halted: "tests_not_run"'), staged)["halted"]
    assert halted and halted["halted"] == "tests_not_run"


def test_arm_d_an_ue_automation_report_discharges_the_owed_row(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import _handler, em_discharge_closes, read_em_discharges
    from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    plan.write_text(_plan(["Plugin/Source/Foo.cpp"], _UE_ROW), encoding="utf-8", newline="")
    index = tmp_path / "ue" / "index.json"
    index.parent.mkdir()
    index.write_text(
        json.dumps({"tests": [{"fullTestPath": "Project.Foo.Spawns", "testDisplayName": "Spawns", "state": "Success"}]}),
        encoding="utf-8",
    )
    params = {
        "verb": "em-discharge", "plan_path": str(plan), "id": "E1", "text": "ran in editor",
        "ue_report": {"index_json": str(index), "tests": ["Project.Foo.Spawns"]},
    }
    result = asyncio.run(_handler(params, repo_root=repo / ".git"))
    assert result["exit_code"] == 0 and result["applied"] is True, result

    entry = read_em_discharges(plan)["E1"]
    assert "index.json" in entry["evidence"] and "Project.Foo.Spawns" in entry["evidence"]
    row = {"id": "E1", "performer": "em", "change_kind": "code-edit"}
    assert em_discharge_closes(row, entry) is True


def test_arm_d_a_failed_ue_test_is_refused_and_records_nothing(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import _handler, read_em_discharges
    from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    plan.write_text(_plan(["Plugin/Source/Foo.cpp"], _UE_ROW), encoding="utf-8", newline="")
    index = tmp_path / "ue" / "index.json"
    index.parent.mkdir()
    index.write_text(json.dumps({"tests": [{"fullTestPath": "Project.Foo.Spawns", "state": "Fail"}]}), encoding="utf-8")
    params = {
        "verb": "em-discharge", "plan_path": str(plan), "id": "E1", "text": "ran in editor",
        "ue_report": {"index_json": str(index), "tests": ["Project.Foo.Spawns"]},
    }
    result = asyncio.run(_handler(params, repo_root=repo / ".git"))
    assert result["exit_code"] != 0 and "Project.Foo.Spawns" in json.dumps(result), result
    assert "E1" not in read_em_discharges(plan)


def test_arm_b_python_run_whose_selected_tests_ran_zero_still_halts_test_infra_failure(tmp_path, script):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    manifest = _staged(tmp_path, _plan(["pkg/a.py"], _UE_ROW))
    assert manifest.em_owed_test_criterion is None and manifest.test_targets
    zero = "{ status: 'pass', tests_run: 0, sidecar_path: 'a.md' }"

    halted = _run(_line(script, 'halted: "test_verdict_empty"'), manifest.to_json(), _testResult=zero)["halted"]
    assert halted and halted["halted"] == "test_infra_failure" and "ran zero tests" in halted["detail"]
