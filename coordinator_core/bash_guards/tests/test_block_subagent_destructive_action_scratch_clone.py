"""A subagent may `git clone` into its own repo's scratch/, including via a cd or -C that stays in that repo."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "scratch").mkdir()
    (root / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "other" / ".git").mkdir(parents=True)
    return root


def _run(command, cwd):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess-clone",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return guard.check(payload)


def _p(path):
    return str(path).replace("\\", "/")


CLONE = "git clone --shared -n . scratch/c0-base/c"


def test_literal_clone_from_repo_root_is_allowed(repo):
    assert _run(CLONE, repo) is None


def test_clone_from_a_subdirectory_into_repo_scratch_is_allowed(repo):
    assert _run(f"git clone --shared -n {_p(repo)} {_p(repo)}/scratch/c0-base/c", repo / "sub" / "deep") is None


def test_cd_into_the_same_repo_then_clone_is_allowed(repo):
    assert _run(f"cd {_p(repo)} && {CLONE}", repo) is None
    assert _run(f"cd {_p(repo)} && {CLONE}", repo / "sub") is None
    assert _run(f"cd {_p(repo)}; {CLONE}", repo) is None


def test_dash_c_to_the_same_repo_clone_is_allowed(repo):
    assert _run(f"git -C {_p(repo)} clone --shared -n . scratch/c0-base/c", repo) is None
    assert _run(f"git -C {_p(repo)} clone --shared -n . {_p(repo)}/scratch/c0-base/c", repo / "sub") is None


def test_empty_or_missing_cwd_does_not_allow_a_clone(repo):
    assert _run(CLONE, None) is not None
    assert _run(CLONE, "") is not None


def test_clone_outside_repo_scratch_is_denied(repo):
    assert _run("git clone --shared -n . sub/c", repo) is not None
    assert _run("git clone --shared -n . c", repo) is not None
    assert _run("git clone --shared -n . ../escape", repo) is not None
    assert _run(f"git clone --shared -n . {_p(repo.parent)}/elsewhere", repo) is not None


def test_repoint_to_another_repo_keeps_the_clone_denied(repo):
    other = repo.parent / "other"
    assert _run(f"cd {_p(other)} && {CLONE}", repo) is not None
    assert _run(f"git -C {_p(other)} clone --shared -n . scratch/c0-base/c", repo) is not None
    assert _run(f"cd {_p(repo)} && cd {_p(other)} && {CLONE}", repo) is not None
    assert _run(f"cd .. && {CLONE}", repo) is not None


def test_cd_into_a_nested_repo_under_the_same_root_stays_denied(repo):
    (repo / "sub" / "nested" / ".git").mkdir(parents=True)
    assert _run(f"cd {_p(repo)}/sub/nested && {CLONE}", repo) is not None


def test_git_dir_and_work_tree_repoints_stay_denied(repo):
    other = repo.parent / "other"
    assert _run(f"git --git-dir={_p(other)}/.git clone . scratch/c0-base/c", repo) is not None
    assert _run(f"git --work-tree={_p(other)} clone . scratch/c0-base/c", repo) is not None


def test_cwd_inside_an_existing_scratch_clone_still_clones_only_into_main_scratch(repo):
    clone = repo / "scratch" / "c0-base" / "c"
    (clone / ".git").mkdir(parents=True)
    assert _run(f"git clone --shared -n . {_p(repo)}/scratch/c1/c", clone) is None
    assert _run(f"git clone --shared -n . {_p(repo)}/sub/escape", clone) is not None


def test_unrelated_cd_segment_in_the_compound_keeps_the_context(repo):
    assert _run(f"cd {_p(repo)}/sub && cd {_p(repo)}/sub && {CLONE.replace('scratch/', _p(repo) + '/scratch/')}", repo) is None
    assert _run(f"cd /nonexistent-dir-xyz && {CLONE}", repo) is not None


def test_refused_clone_options_stay_denied_inside_repo(repo):
    assert _run("git clone --template=/x . scratch/c0-base/c", repo) is not None
    assert _run("git clone -c core.hooksPath=/x . scratch/c0-base/c", repo) is not None


def test_option_like_cd_target_is_not_a_same_repo_repoint(repo):
    assert _run(f"cd - && {CLONE}", repo) is not None
    assert _run(f"cd -P sub && {CLONE}", repo) is not None
    assert _run(f"cd -P {_p(repo)} && {CLONE}", repo) is not None
    assert _run(f"cd {_p(repo)} && {CLONE}", repo) is None
