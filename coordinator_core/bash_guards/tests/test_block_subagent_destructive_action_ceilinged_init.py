"""A subagent may `git init` and write via `git -C` in a temp directory under a declared
GIT_CEILING_DIRECTORIES ancestor; any target that reaches a real repository stays refused."""

from __future__ import annotations

import tempfile

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard


@pytest.fixture
def world(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    work = temp / "work"
    work.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "scratch").mkdir()
    for var in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(var, str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    return work, repo, temp


def _run(command, cwd):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": command.replace("\\", "/")},
            "session_id": "sess-ceil",
            "cwd": str(cwd),
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
        }
    )


# The session scratchpad (`<temp>/claude/<project>/<session_id>/scratchpad/`) replaced the declared
# GIT_CEILING_DIRECTORIES temp dir as the place a subagent may init and write fixture repos
# (see `_scratchpad_fixture_init` / `_scratchpad_repo_verdict`); a ceiling alone no longer admits.
def _pad(temp):
    pad = temp / "claude" / "proj" / "sess-ceil" / "scratchpad"
    pad.mkdir(parents=True)
    return pad


def test_init_under_the_session_scratchpad_is_allowed_a_ceiling_alone_is_not(world):
    work, repo, temp = world
    pad = _pad(temp)
    assert _run(f"git init {pad / 'fx'}", repo) is None
    assert _run("git init fx", pad) is None
    assert _run(f"GIT_CEILING_DIRECTORIES={temp} git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES={temp} git init fx", work) is not None


def test_init_with_exported_or_process_ceiling_does_not_admit_outside_the_scratchpad(world, monkeypatch):
    work, repo, temp = world
    assert _run(f"export GIT_CEILING_DIRECTORIES={temp} && git init {work / 'fx'}", repo) is not None
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(temp))
    assert _run(f"git init {work / 'fx'}", repo) is not None


def test_dash_c_writes_into_scratchpad_fixture_repos_are_allowed(world):
    work, repo, temp = world
    fx = _pad(temp) / "fx"
    (fx / ".git").mkdir(parents=True)
    assert _run(f"git -C {fx} add .", repo) is None
    assert _run(f"git -C {fx} commit -m x", repo) is None
    assert _run(f"git -C {fx} config user.name t", repo) is None
    assert _run(f"git -C {fx} config core.hooksPath /x", repo) is not None


def test_init_without_or_with_wrong_ceiling_is_refused(world):
    work, repo, temp = world
    assert _run(f"git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES= git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES=relative git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES={work / 'other'} git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES={work / 'fx'} git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES={temp.parent} git init {work / 'fx'}", repo) is not None


def test_init_with_a_real_repo_between_target_and_ceiling_is_refused(world):
    work, repo, temp = world
    (work / ".git").mkdir()
    assert _run(f"GIT_CEILING_DIRECTORIES={temp} git init {work / 'fx'}", repo) is not None
    assert _run(f"GIT_CEILING_DIRECTORIES={temp} git init fx", work) is not None


def test_init_inside_the_session_repo_is_refused_despite_ceiling(world):
    work, repo, temp = world
    ceil = f"GIT_CEILING_DIRECTORIES={repo.parent} "
    assert _run(ceil + f"git init {repo / 'sub'}", repo) is not None
    assert _run(ceil + "git init", repo) is not None


def test_init_after_repoint_or_with_template_is_refused(world):
    work, repo, temp = world
    pre = f"GIT_CEILING_DIRECTORIES={temp} "
    assert _run(pre + f"cd {work} && git init fx", repo) is not None
    assert _run(pre + f"git init --template={repo} {work / 'fx'}", repo) is not None
    assert _run(pre + f"GIT_DIR={repo / '.git'} git init {work / 'fx'}", repo) is not None


def test_dash_c_into_real_repo_or_unceilinged_is_refused(world):
    work, repo, temp = world
    fx = work / "fx"
    (fx / ".git").mkdir(parents=True)
    pre = f"GIT_CEILING_DIRECTORIES={temp} "
    assert _run(pre + f"git -C {repo} add .", work) is not None
    assert _run(pre + f"git -C {repo} commit -m x", work) is not None
    assert _run(f"git -C {fx} add .", repo) is not None
    (work / ".git").mkdir()
    assert _run(pre + f"git -C {fx} add .", repo) is not None
