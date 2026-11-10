"""Pins coordinator_core.session_hub_path: hub placement and the leaf-import contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from coordinator_core import session_hub_path as shp


def test_normal_repo_hub_is_outside_dot_git(tmp_path):
    assert shp.hub_for_common(tmp_path / ".git") == tmp_path / ".coordinator-local" / "coordinator-sessions"


def test_bare_common_dir_keeps_hub_inside(tmp_path):
    common = tmp_path / "repo.git"
    assert shp.hub_for_common(common) == common / "coordinator-sessions"


def test_session_dir_for(tmp_path):
    assert shp.session_dir_for(tmp_path / ".git", "sid") == (
        tmp_path / ".coordinator-local" / "coordinator-sessions" / "sid"
    )


def test_hub_for_root_in_repo_and_outside(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert shp.hub_for_root(str(repo)) == repo / ".coordinator-local" / "coordinator-sessions"
    outside = tmp_path / "plain"
    outside.mkdir()
    assert shp.hub_for_root(str(outside)) is None


def test_sessions_dir_in_git_repo_uses_coordinator_local(tmp_path):
    from coordinator_core.session import core

    core.reset_sessions_dir_cache()
    (tmp_path / ".git").mkdir()
    assert core.sessions_dir(cwd=str(tmp_path)) == str(
        tmp_path / ".coordinator-local" / "coordinator-sessions"
    )


def test_module_is_a_leaf():
    code = (
        "import sys; import coordinator_core.session_hub_path;"
        "sys.exit(1 if 'coordinator_core.session.core' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parents[2])).returncode == 0
