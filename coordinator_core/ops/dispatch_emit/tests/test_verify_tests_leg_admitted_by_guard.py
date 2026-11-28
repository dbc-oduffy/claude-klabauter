"""The ask verify-tests leg's commands are admitted by check-test-suite-invocation, and a guard denial of the
leg command reshapes once into the node-id launcher, never twice."""
from __future__ import annotations

import types

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as suite
from coordinator_core.bash_guards import guard_heavy_command_admission as heavy
from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.engine_command import engine_module_command
from coordinator_core.session import touch_record

TARGETS = ["coordinator_core/ops/tests/test_test_leg_run.py", "coordinator_core/ops/tests/test_test_leg_run.py::test_x"]
_TRANSCRIPT = "projects/p/sess/subagents/workflows/wf_abc/agent-a1.jsonl"
AGENTS = ["coordinator:test-runner", "coordinator:executor"]


@pytest.fixture(autouse=True)
def healthy_box(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(heavy, "_read_config", lambda: {"heavy_admission.free_ram_floor_mb": "4096"})
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=32000, source="t")
    )
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: None)
    monkeypatch.setattr(heavy, "_append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        touch_record, "project_live_claims", lambda *a, **k: touch_record.TouchProjection(claims={}, degraded=False)
    )


def _payload(repo, command, agent_type, tool="Bash"):
    return {
        "tool_name": tool, "session_id": "sess-A", "agent_id": "a1", "agent_type": agent_type,
        "cwd": str(repo), "transcript_path": _TRANSCRIPT, "tool_input": {"command": command},
    }


def _denied(result) -> bool:
    return result is not None and getattr(result, "verdict_kind", None) == "deny"


def _quoted(targets) -> str:
    return " ".join(f"'{t}'" for t in targets)


@pytest.mark.parametrize("agent_type", AGENTS)
@pytest.mark.parametrize("shell,tool", [("bash", "Bash"), ("powershell", "PowerShell")])
def test_the_default_leg_command_built_from_touched_ids_is_admitted(tmp_path, agent_type, shell, tool):
    command = engine_module_command(
        "coordinator_core.ops.test_leg_run", str(tmp_path),
        f"--base-sha abc --repo-root \"{tmp_path.as_posix()}\" --out /run/o.json -- {_quoted(TARGETS)}", shell=shell,
    )
    assert not _denied(suite.check(_payload(tmp_path, command, agent_type, tool)))


@pytest.mark.parametrize("module_form", [False, True])
@pytest.mark.parametrize("agent_type", AGENTS)
def test_the_reshaped_node_id_launcher_is_admitted(tmp_path, agent_type, module_form):
    if module_form:
        launcher = tmp_path / "coordinator_core" / "testing" / "scoped_pytest.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("")
    assert (" -m " in emit._launcher_head(tmp_path)) is module_form
    allows = " ".join(f"--allow '{t}'" for t in TARGETS)
    command = f"{emit._launcher_head(tmp_path)} {allows} -- {_quoted(TARGETS)}"
    assert not _denied(suite.check(_payload(tmp_path, command, agent_type)))


@pytest.mark.parametrize("command", ["python -m pytest", "python -m pytest coordinator_core/ops/tests/"])
@pytest.mark.parametrize("agent_type", AGENTS)
def test_broad_shapes_stay_denied(tmp_path, command, agent_type):
    assert _denied(suite.check(_payload(tmp_path, command, agent_type)))


def _call() -> str:
    return ask_compose._verify_tests_call(
        head="H", anchor="A", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), run_id="r1",
        repo_root="REPO", out_path="/run/test-leg.json", base_sha="abc123",
    )


def test_the_prompt_reshapes_once_through_the_launcher_over_the_manifest_targets():
    call = _call()
    assert emit._launcher_head("REPO") in call
    assert "'--allow ' + " in call and "(_manifest.test_targets ?? []).map(" in call
    assert ask_compose.GUARD_REFUSED_CAUSE in call


def test_the_reshape_clause_names_no_unscoped_or_directory_shape():
    clause = "".join(ask_compose._verify_tests_reshape_parts("REPO", "/run/o.json"))
    assert "-m pytest" not in clause
    assert clause.count("'--allow '") == 2
