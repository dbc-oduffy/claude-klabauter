"""Parity of the engine's SYNC_RETURN_TYPES exemption with the plugin's copy."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core import _fleet_names
from coordinator_core.hooks.support import foreground_dispatch_strip as fds

_PLUGIN_REL = "coordinator/hooks/scripts/_foreground_dispatch_strip.py"
SID = "sess-parity-1"


def _doctrine_root() -> Path | None:
    name = _fleet_names.doctrine_repo_name()
    if not name:
        return None
    for raw in _fleet_names._registry_paths():
        if _fleet_names._leaf(raw).casefold() == name.casefold():
            return Path(raw)
    try:
        from coordinator_core._settings_home import claude_config_dir

        raw = (claude_config_dir() / _fleet_names._POINTER).read_text(encoding="utf-8").strip()
        return Path(raw) if raw else None
    except Exception:
        return None


def _plugin_set() -> frozenset:
    root = _doctrine_root()
    if root is None or not (root / _PLUGIN_REL).is_file():
        pytest.skip("no doctrine checkout resolves (consumer box)")
    tree = ast.parse((root / _PLUGIN_REL).read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "SYNC_RETURN_TYPES" for t in node.targets)
        ):
            call = node.value
            assert isinstance(call, ast.Call) and call.args
            return frozenset(ast.literal_eval(call.args[0]))
    pytest.fail("plugin module has no SYNC_RETURN_TYPES assignment")


def test_engine_set_equals_plugin_set():
    assert fds.SYNC_RETURN_TYPES == _plugin_set()


@pytest.fixture
def calibrated(tmp_path, monkeypatch):
    git_dir = tmp_path / "gitdir"
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(git_dir))
    marker = fds._bg_capable_path(str(git_dir), SID)
    marker.parent.mkdir(parents=True)
    marker.touch()
    return git_dir


@pytest.mark.parametrize("member", sorted(fds.SYNC_RETURN_TYPES))
@pytest.mark.parametrize("prefix", ["", "coordinator:"])
def test_member_foreground_passes_through(calibrated, member, prefix):
    ti = {"prompt": "x", "subagent_type": prefix + member}
    assert fds.compute_foreground_reroute(None, SID, ti, "/ignored") is None


@pytest.mark.parametrize("stype", ["executor", "code-reviewer", "coordinator:executor"])
def test_non_member_still_reroutes(calibrated, stype):
    ti = {"prompt": "x", "subagent_type": stype}
    result = fds.compute_foreground_reroute(None, SID, ti, "/ignored")
    assert result is not None and result[0] == "reroute" and result[1] is True


def test_absent_param_uncalibrated_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(fds, "_resolve_git_dir", lambda cwd: str(tmp_path / "g"))
    ti = {"prompt": "x", "subagent_type": "executor"}
    assert fds.compute_foreground_reroute(None, SID, ti, "/ignored") is None
