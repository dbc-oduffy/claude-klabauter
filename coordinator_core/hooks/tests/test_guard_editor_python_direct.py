"""Tests for hooks.guard_editor_python_direct."""

from __future__ import annotations

import json

import pytest

from coordinator_core.hooks import guard_editor_python_direct as g

UE_TOOL = "mcp__example_game_repo-control__execute_python_code"
UE_CMD = "UnrealEditor-Cmd.exe proj.uproject -run=pythonscript -script=x.py"


def _table(editor):
    data = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "ue",
                "match": {"keywords": ["unreal"]},
                "agent_type": "example-game-repo-control:ue-author",
                "on_mismatch": "refuse",
            }
        ],
    }
    if editor is not None:
        data["editor_python"] = editor
    return data


def _repo(tmp_path, editor, *, raw=None):
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else json.dumps(_table(editor))
    (tmp_path / ".claude" / "route-table.json").write_text(text, encoding="utf-8")
    return tmp_path


def _payload(tmp_path, tool, **extra):
    p = {"tool_name": tool, "tool_input": {}, "cwd": str(tmp_path)}
    p.update(extra)
    return p


def _editor(level="warn", **kw):
    base = {"preferred_agent_type": "example-game-repo-control:ue-python", "level": level}
    base.update(kw)
    return base


def _ctx(out):
    return out["hookSpecificOutput"]["additionalContext"]


def test_em_tool_call_advises_preferred_agent(tmp_path):
    _repo(tmp_path, _editor(tool_names=[UE_TOOL]))
    out = g.evaluate(_payload(tmp_path, UE_TOOL))
    assert "editor_python.preferred_agent_type" in _ctx(out)
    assert "example-game-repo" not in _ctx(out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_subagent_call_is_untouched(tmp_path):
    _repo(tmp_path, _editor(tool_names=[UE_TOOL]))
    assert g.evaluate(_payload(tmp_path, UE_TOOL, agent_id="a1")) == {}


def test_bash_pattern_match_advises(tmp_path):
    _repo(tmp_path, _editor(bash_patterns=["-run=pythonscript"]))
    p = _payload(tmp_path, "Bash", tool_input={"command": UE_CMD})
    assert "editor_python.preferred_agent_type" in _ctx(g.evaluate(p))


ANCHORED = r"^\S*UnrealEditor(-Cmd)?(\.exe)?\s.*-run=pythonscript"
UNANCHORED = "-run=pythonscript"
HEREDOC = "python - <<'EOF'\nUnrealEditor-Cmd p.uproject -run=pythonscript\nEOF"


def _bash(tmp_path, command, level="warn", pattern=ANCHORED):
    _repo(tmp_path, _editor(level, bash_patterns=[pattern]))
    return g.evaluate(_payload(tmp_path, "Bash", tool_input={"command": command}))


@pytest.mark.parametrize(
    "command",
    [
        "UnrealEditor-Cmd.exe proj.uproject -run=pythonscript -script=x.py",
        'timeout 600 "/UE/UnrealEditor-Cmd.exe" p.uproject -run=python""script',
        "cd proj && UnrealEditor-Cmd p.uproject -run=pythonscript",
    ],
)
def test_bash_launch_advises(tmp_path, command):
    out = _bash(tmp_path, command)
    assert "editor_python.preferred_agent_type" in _ctx(out)


@pytest.mark.parametrize(
    "command",
    [
        "git log --grep 'UnrealEditor-Cmd p.uproject -run=pythonscript'",
        "echo hi # UnrealEditor-Cmd -run=pythonscript",
        HEREDOC,
    ],
)
def test_bash_benign_text_is_untouched(tmp_path, command):
    assert _bash(tmp_path, command) == {}


@pytest.mark.parametrize("command", ["echo hi # UnrealEditor-Cmd -run=pythonscript", HEREDOC])
def test_unanchored_pattern_still_skips_comment_and_heredoc(tmp_path, command):
    assert _bash(tmp_path, command, pattern=UNANCHORED) == {}


def test_bash_leg_at_strict_is_advisory_never_deny(tmp_path):
    out = _bash(tmp_path, UE_CMD, level="strict")
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "editor_python.preferred_agent_type" in _ctx(out)


def test_unresolved_command_is_skipped(tmp_path):
    assert _bash(tmp_path, "$LAUNCH p.uproject -run=pythonscript", pattern=UNANCHORED) == {}


def test_unrelated_bash_is_untouched(tmp_path):
    _repo(tmp_path, _editor(bash_patterns=["-run=pythonscript"]))
    p = _payload(tmp_path, "Bash", tool_input={"command": "ls -la"})
    assert g.evaluate(p) == {}


def test_bash_only_table_ignores_other_tools(tmp_path):
    _repo(tmp_path, _editor(bash_patterns=["-run=pythonscript"]))
    assert g.evaluate(_payload(tmp_path, UE_TOOL)) == {}


def test_strict_denies(tmp_path, monkeypatch):
    from coordinator_core import machine_profile

    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "strict")
    _repo(tmp_path, _editor("strict", tool_names=[UE_TOOL]))
    out = g.evaluate(_payload(tmp_path, UE_TOOL))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "editor_python.preferred_agent_type" in json.dumps(out)


def test_no_table_is_untouched(tmp_path):
    (tmp_path / ".git").mkdir()
    assert g.evaluate(_payload(tmp_path, UE_TOOL)) == {}


def test_no_editor_python_is_untouched(tmp_path):
    _repo(tmp_path, None)
    assert g.evaluate(_payload(tmp_path, UE_TOOL)) == {}


def test_malformed_table_fails_open(tmp_path):
    _repo(tmp_path, None, raw="{not json")
    assert g.evaluate(_payload(tmp_path, UE_TOOL)) == {}


def test_explicit_repo_root_wins(tmp_path):
    root = _repo(tmp_path / "r", _editor(tool_names=[UE_TOOL]))
    out = g.evaluate({"tool_name": UE_TOOL, "cwd": str(tmp_path)}, repo_root=root)
    assert out != {}


def test_handler_delegates(tmp_path):
    _repo(tmp_path, _editor(tool_names=[UE_TOOL]))
    assert g._handler(_payload(tmp_path, UE_TOOL)) != {}


def test_no_subprocess(tmp_path, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _repo(tmp_path, _editor(tool_names=[UE_TOOL]))
    assert g.evaluate(_payload(tmp_path, UE_TOOL)) != {}


@pytest.mark.parametrize("bad", [None, "x", 3])
def test_bad_payload_fails_open(bad):
    assert g.evaluate(bad) == {}
