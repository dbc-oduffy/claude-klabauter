"""Tool-surface hook gates resolve their deny at the guard-policy point."""

import pytest

from coordinator_core import machine_profile
from coordinator_core.hooks import block_worktree_tool, guard_python_syntax_on_write

_GLOBAL = "MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL"
_WORKTREE_KEY = _GLOBAL + "_BLOCK-WORKTREE-TOOL"
_SYNTAX_KEY = _GLOBAL + "_GUARD-PYTHON-SYNTAX-ON-WRITE"

_ENTER = {"tool_name": "EnterWorktree"}


def _bad_write(tmp_path):
    target = tmp_path / "coordinator_core" / "broken.py"
    target.parent.mkdir()
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "def (:\n"},
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    reg = tmp_path / "reg"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.delenv(_GLOBAL, raising=False)
    machine_profile.reset_cache()
    yield monkeypatch
    machine_profile.reset_cache()


def _denies(result):
    return (result.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


def _flag(mp, key, value):
    mp.setenv(key, value)
    machine_profile.reset_cache()


def test_no_flags_warns_not_denies(isolated, tmp_path):
    out = block_worktree_tool._handler(dict(_ENTER))
    assert not _denies(out)
    assert out["hookSpecificOutput"]["additionalContext"]
    out = guard_python_syntax_on_write._handler(_bad_write(tmp_path))
    assert not _denies(out)
    assert out["hookSpecificOutput"]["additionalContext"]


def test_global_strict_denies(isolated, tmp_path):
    _flag(isolated, _GLOBAL, "strict")
    assert _denies(block_worktree_tool._handler(dict(_ENTER)))
    assert _denies(guard_python_syntax_on_write._handler(_bad_write(tmp_path)))


def test_per_guard_strict_hardens_one_leg_only(isolated, tmp_path):
    _flag(isolated, _GLOBAL, "warn")
    _flag(isolated, _SYNTAX_KEY, "strict")
    assert _denies(guard_python_syntax_on_write._handler(_bad_write(tmp_path)))
    assert not _denies(block_worktree_tool._handler(dict(_ENTER)))


def test_per_guard_off_allows_silently(isolated):
    _flag(isolated, _WORKTREE_KEY, "off")
    assert not _denies(block_worktree_tool._handler(dict(_ENTER)))


@pytest.mark.parametrize(
    "modname",
    [
        "block_worktree_tool",
        "check_claude_md_size",
        "guard_doctrine_changelog_prose",
        "guard_doctrine_surface_bash_write",
        "guard_doctrine_surface_ratio",
        "guard_python_syntax_on_write",
        "guard_repo_setup_claude_home_refusal",
    ],
)
def test_guard_name_is_kebab_of_module(modname):
    import importlib

    mod = importlib.import_module("coordinator_core.hooks." + modname)
    assert mod.GUARD_NAME == modname.replace("_", "-")
