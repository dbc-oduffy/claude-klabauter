"""headless-claude-plugin-dir: rewrite, pass-through, and unresolvable-root deny."""

import pytest

from coordinator_core.bash_guards import guard_headless_claude_plugin_dir as g

ROOT = "/native/coordinator"


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(g, "_plugin_root", lambda: ROOT)


def _rewritten(cmd):
    out = g.check_headless_claude_plugin_dir(cmd)
    assert out is not None
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow"
    assert "--plugin-dir" in spec["additionalContext"]
    return spec["updatedInput"]["command"]


def test_bare_print_rewritten():
    assert _rewritten('claude -p "x"') == 'claude --plugin-dir /native/coordinator -p "x"'


def test_exe_long_flag_rewritten():
    assert _rewritten("claude.exe --print hi") == "claude.exe --plugin-dir /native/coordinator --print hi"


def test_after_and_and_pipe_rewritten():
    assert _rewritten("cd d && claude -p x | cat") == "cd d && claude --plugin-dir /native/coordinator -p x | cat"


def test_path_and_env_prefix_rewritten():
    assert _rewritten("FOO=1 /usr/bin/claude -p x") == "FOO=1 /usr/bin/claude --plugin-dir /native/coordinator -p x"


@pytest.mark.parametrize(
    "cmd",
    [
        "claude -p x --plugin-dir /p",
        "claude --plugin-dir=/p -p x",
        "claude-author -p x",
        "claude-author.cmd -p x",
        "claude-author.ps1 --print x",
        "claude-author.py -p x",
        'echo "claude -p"',
        "grep 'claude -p' f",
        "claude --version",
        "ls",
    ],
)
def test_pass_through(cmd):
    assert g.check_headless_claude_plugin_dir(cmd) is None


def test_unresolvable_root_denies(monkeypatch):
    monkeypatch.setattr(g, "_plugin_root", lambda: None)
    out = g.check_headless_claude_plugin_dir("claude -p x")
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert "plugin root" in spec["permissionDecisionReason"]
