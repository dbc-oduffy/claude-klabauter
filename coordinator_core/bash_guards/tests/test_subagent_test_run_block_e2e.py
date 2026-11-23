"""End-to-end through the installed chain entry (`dispatch.evaluate_payload_json`): a subagent other
than the test-runner is refused every pytest shape, the test-runner runs touched node ids only, the
ceiling refuses every caller, and one evaluation spawns no process."""

from __future__ import annotations

import json
import subprocess
import types

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_heavy_command_admission as heavy
from coordinator_core.session import touch_record

SESSION = "sess-e2e"
TOUCHED = "coordinator_core/bash_guards/tests/test_touched.py"
TOUCHED_2 = "coordinator_core/bash_guards/tests/test_touched_two.py"
UNTOUCHED = "coordinator_core/bash_guards/tests/test_other.py"
RUNNER = "coordinator:test-runner"

# The five pytest shapes seen on the live box (ledger R2).
R2_SHAPES = {
    "multi-module-n1-junitxml": (
        "python -m pytest -n 1 %s %s --junitxml=r47-scope-resume.xml" % (TOUCHED, TOUCHED_2)
    ),
    "multi-file-node-list-n1": "python -m pytest -n 1 %s::test_a %s::test_b" % (TOUCHED, TOUCHED_2),
    "timeout-900-four-files": "timeout 900 pytest %s %s %s %s" % (TOUCHED, TOUCHED_2, UNTOUCHED, TOUCHED),
    "single-file-under-timeout": "timeout 110 python -m pytest %s" % TOUCHED,
    "engine-verify-n4": "python -m pytest -n 4 %s" % TOUCHED,
}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def git_repo(tmp_path):
    """A real repository: the git-root cache fills only from a successful `git rev-parse`."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture(autouse=True)
def healthy_box(monkeypatch):
    monkeypatch.setattr(heavy, "_read_config", lambda: {"heavy_admission.free_ram_floor_mb": "4096"})
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=32000, source="t")
    )
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: None)


@pytest.fixture
def touched(monkeypatch):
    projection = touch_record.TouchProjection(claims={p: object() for p in (TOUCHED, TOUCHED_2)}, degraded=False)
    monkeypatch.setattr(touch_record, "project_live_claims", lambda *a, **k: projection)


def _evaluate(repo, command, **identity):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": SESSION,
        "cwd": str(repo),
    }
    payload.update(identity)
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _denied(out) -> bool:
    return bool(out) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


EXECUTOR = {"agent_id": "agent-1", "agent_type": "coordinator:executor"}
WORKFLOW_AGENT = {"agent_id": "agent-2"}  # no agent_type: presence of agent_id alone marks a subagent


@pytest.mark.parametrize("identity", [EXECUTOR, WORKFLOW_AGENT], ids=["executor", "workflow-agent"])
@pytest.mark.parametrize("command", list(R2_SHAPES.values()), ids=list(R2_SHAPES))
def test_subagent_that_is_not_the_test_runner_is_refused_every_r2_shape(repo, touched, command, identity):
    out = _evaluate(repo, command, **identity)
    assert _denied(out), out
    assert "test-runner" in json.dumps(out)


def test_test_runner_touched_node_id_is_allowed(repo, touched):
    for command in (
        "python -m pytest %s" % TOUCHED,
        "python -m pytest %s::test_a -q" % TOUCHED,
    ):
        out = _evaluate(repo, command, agent_id="agent-3", agent_type=RUNNER)
        assert not _denied(out), out


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest %s" % UNTOUCHED,
        "python -m pytest coordinator_core/bash_guards/tests",
        "python -m pytest %s -n 2" % TOUCHED,
        "python -m pytest",
    ],
    ids=["untouched-file", "directory", "workers", "bare"],
)
def test_test_runner_outside_touched_scope_is_refused(repo, touched, command):
    out = _evaluate(repo, command, agent_id="agent-3", agent_type=RUNNER)
    assert _denied(out), out


def test_em_node_id_is_refused_below_the_ram_floor(repo, touched, monkeypatch):
    em_command = "python -m pytest %s::test_a" % TOUCHED
    assert not _denied(_evaluate(repo, em_command))
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=1000, source="t")
    )
    out = _evaluate(repo, em_command)
    assert _denied(out), out
    assert "1000 MB" in json.dumps(out)


def test_test_runner_touched_run_is_refused_below_the_ram_floor(repo, touched, monkeypatch):
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=1000, source="t")
    )
    out = _evaluate(repo, "python -m pytest %s" % TOUCHED, agent_id="agent-3", agent_type=RUNNER)
    assert _denied(out), out


@pytest.mark.parametrize(
    "identity",
    [EXECUTOR, {"agent_id": "agent-3", "agent_type": RUNNER}, {}],
    ids=["refused-executor", "admitted-test-runner", "em"],
)
def test_one_warm_evaluation_spawns_no_process(git_repo, touched, monkeypatch, identity):
    """The first evaluation in a repo fills the git-root cache with one `git rev-parse`; the
    evaluation counted here is the warm one."""
    repo = git_repo
    command = "python -m pytest %s" % TOUCHED
    _evaluate(repo, command, **identity)
    spawns = []
    real = subprocess.Popen

    def counting(*args, **kwargs):
        spawns.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting)
    _evaluate(repo, command, **identity)
    assert spawns == []
