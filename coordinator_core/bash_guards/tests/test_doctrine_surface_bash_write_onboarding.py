"""Onboarding creates, file-test reads, and guard_level on the Bash doctrine-surface guard."""
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as g

SURFACES = ["CLAUDE.md", "coordinator.local.md"]


def _check(cmd, cwd):
    return g.check({"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": cmd}}, SURFACES)


@pytest.fixture(autouse=True)
def _strict(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")


@pytest.mark.parametrize(
    "cmd",
    [
        'test -f "$d/coordinator.local.md" >/dev/null',
        'if test -f "$d/coordinator.local.md" 2>/dev/null; then echo y; fi',
        '[ -f coordinator.local.md ] && echo has >/dev/null 2>&1',
        'for d in a b; do test -e "$d/coordinator.local.md" >/dev/null || echo "$d"; done',
    ],
)
def test_file_test_is_not_a_write(tmp_path, cmd):
    assert _check(cmd, tmp_path) is None


def test_file_test_with_a_real_redirect_still_denies(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("x")
    assert _check("test -f coordinator.local.md > coordinator.local.md", tmp_path) is not None


def test_first_time_claude_md_render_is_allowed(tmp_path):
    assert _check("cat tpl > CLAUDE.md", tmp_path) is None


def test_existing_claude_md_write_still_denies(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x")
    assert _check("cat tpl > CLAUDE.md", tmp_path) is not None


def test_project_type_only_local_config_write_is_allowed(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("x")
    assert _check("printf 'project_type: general\\n' > coordinator.local.md", tmp_path) is None


def test_local_config_write_with_command_key_denies(tmp_path):
    assert _check("printf 'project_type: g\\nfast_test_cmd: x\\n' > coordinator.local.md", tmp_path) is not None


@pytest.mark.parametrize("level", ["warn", "off"])
def test_guard_level_relaxes(tmp_path, monkeypatch, level):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
    (tmp_path / "CLAUDE.md").write_text("x")
    out = _check("echo hi >> CLAUDE.md", tmp_path)
    if level == "off":
        assert out is None
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "Doctrine surfaces reach every session" in out["hookSpecificOutput"]["additionalContext"]
