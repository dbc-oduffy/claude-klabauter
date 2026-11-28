"""block_clone_shaped_cruft: a hand-made repo tree copy is denied for a regular subagent and
advised for the EM and a named reviewer; an engine op that owns its delete is never matched."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.bash_guards import block_clone_shaped_cruft as guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.guard_messages.testing import envelope_of

NAME = "block-clone-shaped-cruft"

EM = {}
SUBAGENT = {"agent_id": "deadbeef0123", "agent_type": "coordinator:executor"}
REVIEWER = {"agent_id": "deadbeef0123", "agent_type": "coordinator:code-reviewer"}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "plain" / "sub").mkdir(parents=True)

    def _no_spawn(*a, **k):
        raise AssertionError("guard spawned a process")

    monkeypatch.setattr(subprocess, "run", _no_spawn)
    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    return tmp_path


def _payload(tree, command, identity, tool="Bash"):
    return {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "s1",
        "cwd": str(tree),
        **identity,
    }


def _env(tree, command, identity=SUBAGENT, tool="Bash"):
    return envelope_of(guard.check(_payload(tree, command, identity, tool)))


def _denies(env):
    return bool(env) and env.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _shapes(tree):
    repo = tree / "repo"
    return {
        "clone": f"git clone {repo} {tree / 'x'}",
        "cp": f"cp -r {repo} {tree / 'x'}",
        "cp-archive": f"cp -a {repo}/ {tree / 'x'}",
        "robocopy": f"robocopy {repo} {tree / 'x'} /E",
        "xcopy": f"xcopy {repo} {tree / 'x'} /S /E",
        "archive": f"git archive HEAD | tar -x -C {tree / 'x'}",
        "archive-sub": f"git archive HEAD | (cd {tree / 'x'} && tar xf -)",
    }


@pytest.mark.parametrize("shape", ["clone", "cp", "cp-archive", "robocopy", "xcopy", "archive", "archive-sub"])
def test_a_regular_subagent_is_denied_each_tree_copy_shape(tree, shape):
    env = _env(tree, _shapes(tree)[shape], SUBAGENT)
    assert _denies(env), shape


@pytest.mark.parametrize("identity", [EM, REVIEWER], ids=["em", "named-reviewer"])
@pytest.mark.parametrize("shape", ["clone", "cp", "robocopy", "archive"])
def test_the_em_and_a_named_reviewer_are_warned_not_denied(tree, shape, identity):
    result = guard.check(_payload(tree, _shapes(tree)[shape], identity))
    assert result is not None
    assert result.verdict_kind == "advisory"
    assert not _denies(envelope_of(result))


def test_copy_item_recurse_of_a_repo_is_denied_on_powershell(tree):
    command = f"Copy-Item -Recurse -Path {tree / 'repo'} -Destination {tree / 'x'}"
    assert _denies(_env(tree, command, SUBAGENT, tool="PowerShell"))
    assert not _denies(_env(tree, f"Copy-Item -Path {tree / 'repo'} -Destination {tree / 'x'}", SUBAGENT, tool="PowerShell"))


@pytest.mark.parametrize(
    "command",
    [
        "git clone https://github.com/example/oss.git out",
        "git clone git@github.com:example/oss.git out",
        "git clone {plain} out",
        "cp -r {plain} out",
        "cp {repo}/README out",
        "robocopy {repo} out /L",
        "git archive HEAD -o out.tar",
        "git archive HEAD | wc -c",
        "tar -xf bundle.tar -C out",
        "coordinator-invoke baseline_export --sha abc -- python -m pytest t.py",
        "python -m coordinator_core.testing.baseline_export --sha abc -- pytest t.py",
        "baseline-export --sha abc -- pytest t.py",
        "git status",
    ],
)
def test_non_tree_copy_shapes_and_engine_ops_are_allowed_for_a_subagent(tree, command):
    command = command.format(repo=tree / "repo", plain=tree / "plain")
    assert guard.check(_payload(tree, command, SUBAGENT)) is None


def test_the_deny_record_offers_the_engine_export(tree):
    env = _env(tree, _shapes(tree)["cp"], SUBAGENT)
    assert "baseline-export" in json.dumps(env)


def test_the_guard_is_in_the_production_chain_and_denies_a_subagent_cp(tree, monkeypatch):
    monkeypatch.setenv(dispatch._VERDICT_ONLY_ENV, "1")
    command = _shapes(tree)["cp"]
    chain = dispatch._build_guard_chain(command, "s1", str(tree), _payload(tree, command, SUBAGENT), None, None, None)
    assert NAME in [e.name for e in chain]
    out = dispatch.evaluate_payload_json(json.dumps(_payload(tree, command, SUBAGENT)))
    assert _denies(out)
    em = dispatch.evaluate_payload_json(json.dumps(_payload(tree, command, EM)))
    assert not _denies(em)
