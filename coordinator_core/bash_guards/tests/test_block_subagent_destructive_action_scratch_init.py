"""A subagent may build a fixture git repo under its own session scratchpad; the real repo stays fenced."""

from __future__ import annotations

import tempfile

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard

SESSION = "sess-g3"


@pytest.fixture
def world(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    pad = temp / "claude" / "proj" / SESSION / "scratchpad"
    pad.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tracked").mkdir()
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return pad, repo, temp


def _run(command, cwd, session_id=SESSION):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command.replace("\\", "/")},
            "session_id": session_id,
            "cwd": str(cwd),
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
        }
    )


def test_init_under_the_session_scratchpad_is_allowed(world):
    pad, repo, _ = world
    assert _run(f"git init {pad / 'fixture'}", repo) is None
    assert _run("git init fixture", pad) is None
    assert _run("git init", pad / "fixture") is None


def test_init_inside_the_tracked_tree_is_denied(world):
    _pad, repo, _ = world
    assert _run(f"git init {repo / 'tracked' / 'x'}", repo) is not None
    assert _run("git init", repo) is not None


def test_init_under_another_sessions_scratchpad_is_denied(world):
    pad, repo, temp = world
    other = temp / "claude" / "proj" / "other-sess" / "scratchpad"
    other.mkdir(parents=True)
    assert _run(f"git init {other / 'f'}", repo) is not None
    assert _run(f"git init {pad / 'f'}", repo, session_id="") is not None


def test_init_nested_in_an_existing_scratchpad_repo_is_denied(world):
    pad, repo, _ = world
    (pad / "fixture" / ".git").mkdir(parents=True)
    assert _run(f"git init {pad / 'fixture' / 'sub'}", repo) is not None


def test_init_after_a_cd_is_denied(world):
    pad, repo, _ = world
    assert _run(f"cd {repo} && git init", pad) is not None


def test_add_commit_config_dash_c_into_a_scratchpad_repo_are_allowed(world):
    pad, repo, _ = world
    fx = pad / "fixture"
    (fx / ".git").mkdir(parents=True)
    assert _run(f"git -C {fx} add -A", repo) is None
    assert _run(f"git -C {fx} commit -m seed", repo) is None
    assert _run(f"git -C {fx} config user.email a@b.c", repo) is None


def test_dash_c_into_the_real_repo_is_denied(world):
    pad, repo, _ = world
    assert _run(f"git -C {repo} add -A", pad) is not None
    assert _run(f"git -C {repo} commit -m x", pad) is not None
    assert _run(f"git -C {repo} config user.name x", pad) is not None


def test_scratchpad_repo_config_is_limited_to_identity_keys(world):
    pad, repo, _ = world
    fx = pad / "fixture"
    (fx / ".git").mkdir(parents=True)
    assert _run(f"git -C {fx} config core.hooksPath {repo}", repo) is not None
    assert _run(f"git -C {fx} config alias.x '!sh'", repo) is not None
