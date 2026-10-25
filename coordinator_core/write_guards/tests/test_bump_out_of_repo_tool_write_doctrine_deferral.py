"""bump_out_of_repo_tool_write defers on content-root doctrine paths: one guard
(guard_doctrine_surface_edits) owns doctrine policy; every other out-of-repo
path still denies exactly as before.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.tests.git_seed import seeded_repo
from coordinator_core.write_guards import _gem_doctrine_carve_out as carve_out
from coordinator_core.write_guards import bump_out_of_repo_tool_write as guard

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture
def world(tmp_path, monkeypatch):
    own = seeded_repo(tmp_path / "own-repo", name="Test")
    doe = seeded_repo(tmp_path / "doe-repo", name="Test")
    peer = seeded_repo(tmp_path / "peer-repo", name="Test")
    monkeypatch.setattr(carve_out, "read_content_root", lambda: str(doe))
    monkeypatch.setattr(carve_out, "_content_root_memo", [])
    session_id = "sess-doctrine-deferral"
    session_start.write_session_start_record(session_id, launch_cwd=str(own))
    return {"own": own, "doe": doe, "peer": peer, "sid": session_id}


def _payload(world, path, sid=None, agent_id=""):
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path)},
        "session_id": sid or world["sid"],
        "cwd": str(world["own"]),
        "agent_id": agent_id,
    }


def _doctrine_paths(doe):
    return [
        doe / "global-doctrine" / "CLAUDE.md",
        doe / "coordinator" / "skills" / "group-em" / "SKILL.md",
    ]


@pytest.mark.parametrize("blocked", [True, False])
@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("agent_id", ["", "sub-1"])
def test_doctrine_path_gets_none_for_every_caller(world, monkeypatch, blocked, which, agent_id):
    monkeypatch.setattr(guard, "out_of_repo_write_blocked", lambda: blocked)
    target = _doctrine_paths(world["doe"])[which]
    assert guard.check(_payload(world, target, agent_id=agent_id)) is None


def test_deferral_is_what_clears_the_doctrine_path(world, monkeypatch):
    target = _doctrine_paths(world["doe"])[0]
    monkeypatch.setattr(guard, "out_of_repo_write_blocked", lambda: True)
    assert guard.check(_payload(world, target)) is None
    monkeypatch.setattr(guard, "doctrine_root_of", lambda _p: None)
    refused = guard.check(_payload(world, target))
    assert refused is not None
    assert refused["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("repo", ["doe", "peer"])
def test_non_doctrine_paths_deny_exactly_as_before(world, monkeypatch, repo):
    monkeypatch.setattr(guard, "out_of_repo_write_blocked", lambda: True)
    target = world[repo] / "state" / "x.md"
    now = guard.check(_payload(world, target))
    assert now is not None
    assert now["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setattr(guard, "doctrine_root_of", lambda _p: None)
    assert guard.check(_payload(world, target)) == now


def test_engine_plane_path_still_denies(world, monkeypatch):
    monkeypatch.setattr(guard, "out_of_repo_write_blocked", lambda: True)
    target = world["peer"] / "coordinator" / "skills" / "group-em" / "SKILL.md"
    refused = guard.check(_payload(world, target))
    assert refused is not None
    assert refused["hookSpecificOutput"]["permissionDecision"] == "deny"
