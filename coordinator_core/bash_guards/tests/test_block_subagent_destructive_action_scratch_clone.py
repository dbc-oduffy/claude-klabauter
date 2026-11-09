"""A subagent may `git clone` into its own repo's scratch/ and into any empty or absent directory; an occupied destination stays denied."""

from __future__ import annotations

import subprocess

import pytest

pytestmark = pytest.mark.complete

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


def _occupy(*paths):
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        (path / "keep").write_text("x")


CLONE = "git clone --shared -n . scratch/c0-base/c"


def test_literal_clone_from_repo_root_is_allowed(repo):
    assert _run(CLONE, repo) is None


def test_clone_from_a_subdirectory_into_repo_scratch_is_allowed(repo):
    assert _run(f"git clone --shared -n {_p(repo)} {_p(repo)}/scratch/c0-base/c", repo / "sub" / "deep") is None


def test_cd_into_the_same_repo_then_clone_is_allowed(repo):
    assert _run(f"cd {_p(repo)} && {CLONE}", repo) is None
    assert _run(f"cd {_p(repo)} && {CLONE}", repo / "sub") is None

def test_only_one_leading_cd_and_directly_resolves_the_repo(repo):
    # The guard resolves the repo for a single leading `cd <abs dir> &&` (3b85a0c0df..307ea22403);
    # `;` joiners and `git -C` re-points no longer resolve one, so the clone is refused.
    assert _run(f"cd {_p(repo)}; {CLONE}", repo) is not None
    _occupy(repo / "scratch" / "c0-base" / "c")
    assert _run(f"git -C {_p(repo)} clone --shared -n . scratch/c0-base/c", repo) is not None
    assert _run(f"git -C {_p(repo)} clone --shared -n . {_p(repo)}/scratch/c0-base/c", repo / "sub") is not None


def test_empty_or_missing_cwd_does_not_allow_a_clone(repo):
    assert _run(CLONE, None) is not None
    assert _run(CLONE, "") is not None


def test_clone_into_occupied_dir_outside_repo_scratch_is_denied(repo):
    _occupy(repo / "sub" / "c", repo / "c", repo.parent / "escape", repo.parent / "elsewhere")
    assert _run("git clone --shared -n . sub/c", repo) is not None
    assert _run("git clone --shared -n . c", repo) is not None
    assert _run("git clone --shared -n . ../escape", repo) is not None
    assert _run(f"git clone --shared -n . {_p(repo.parent)}/elsewhere", repo) is not None


def test_clone_after_repoint_is_allowed_only_into_the_named_repos_own_scratch(repo):
    other = repo.parent / "other"
    # A single leading cd lands in that repo's own scratch/ -- the clone stays inside the repo it names.
    assert _run(f"cd {_p(other)} && {CLONE}", repo) is None
    assert _run(f"git -C {_p(other)} clone --shared -n . scratch/c0-base/c", repo) is not None
    assert _run(f"cd {_p(repo)} && cd {_p(other)} && {CLONE}", repo) is not None
    assert _run(f"cd .. && {CLONE}", repo) is not None


def test_cd_into_a_nested_repo_clones_into_the_nested_repos_own_scratch(repo):
    (repo / "sub" / "nested" / ".git").mkdir(parents=True)
    assert _run(f"cd {_p(repo)}/sub/nested && {CLONE}", repo) is None
    _occupy(repo / "scratch" / "c0-base" / "c")
    assert _run(f"cd {_p(repo)}/sub/nested && git clone --shared -n . {_p(repo)}/scratch/c0-base/c", repo) is not None


def test_git_dir_and_work_tree_repoints_stay_denied(repo):
    other = repo.parent / "other"
    assert _run(f"git --git-dir={_p(other)}/.git clone . scratch/c0-base/c", repo) is not None
    assert _run(f"git --work-tree={_p(other)} clone . scratch/c0-base/c", repo) is not None


def test_cwd_inside_an_existing_scratch_clone_still_clones_only_into_main_scratch(repo):
    clone = repo / "scratch" / "c0-base" / "c"
    (clone / ".git").mkdir(parents=True)
    assert _run(f"git clone --shared -n . {_p(repo)}/scratch/c1/c", clone) is None
    _occupy(repo / "sub" / "escape")
    assert _run(f"git clone --shared -n . {_p(repo)}/sub/escape", clone) is not None


def test_a_second_cd_in_the_compound_refuses_the_clone(repo):
    _occupy(repo / "scratch" / "c0-base" / "c")
    assert _run(f"cd {_p(repo)}/sub && cd {_p(repo)}/sub && {CLONE.replace('scratch/', _p(repo) + '/scratch/')}", repo) is not None
    assert _run(f"cd /nonexistent-dir-xyz && {CLONE}", repo) is not None


def test_refused_clone_options_stay_denied_inside_repo(repo):
    assert _run("git clone --template=/x . scratch/c0-base/c", repo) is not None
    assert _run("git clone -c core.hooksPath=/x . scratch/c0-base/c", repo) is not None


def test_option_like_cd_target_is_not_a_same_repo_repoint(repo):
    assert _run(f"cd - && {CLONE}", repo) is not None
    assert _run(f"cd -P sub && {CLONE}", repo) is not None
    assert _run(f"cd -P {_p(repo)} && {CLONE}", repo) is not None
    assert _run(f"cd {_p(repo)} && {CLONE}", repo) is None


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", cwd=src)
    (src / "a.txt").write_text("a")
    _git("add", "a.txt", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    return src


def test_real_clone_into_empty_dir_outside_scratch_passes_and_runs(repo, source, tmp_path):
    dest = tmp_path / "out" / "empty"
    dest.mkdir(parents=True)
    cmd = f"git clone {_p(source)} {_p(dest)}"
    assert _run(cmd, repo) is None
    subprocess.run(cmd, shell=True, check=True, capture_output=True)
    assert (dest / "a.txt").read_text() == "a"


def test_real_clone_into_absent_dir_outside_scratch_passes_and_runs(repo, source, tmp_path):
    dest = tmp_path / "out" / "absent"
    cmd = f"git clone {_p(source)} {_p(dest)}"
    assert _run(cmd, repo) is None
    subprocess.run(cmd, shell=True, check=True, capture_output=True)
    assert (dest / "a.txt").exists()


def test_relative_absent_dest_outside_scratch_passes(repo, source):
    assert _run(f"git clone {_p(source)} fresh", repo) is None


def test_clone_into_non_empty_dir_is_denied_naming_the_rule(repo, source, tmp_path):
    dest = tmp_path / "out" / "full"
    _occupy(dest)
    result = _run(f"git clone {_p(source)} {_p(dest)}", repo)
    assert result is not None
    assert "empty or absent" in result.summary


def test_clone_onto_an_existing_file_is_denied(repo, source, tmp_path):
    target = tmp_path / "afile"
    target.write_text("x")
    assert _run(f"git clone {_p(source)} {_p(target)}", repo) is not None


@pytest.mark.parametrize("opt", ["--template=/x", "--separate-git-dir=/x/g", "-u /x", "--upload-pack=/x", "-c core.hooksPath=/x"])
def test_refused_options_stay_refused_for_an_empty_dest(repo, source, tmp_path, opt):
    dest = tmp_path / "out" / "empty2"
    dest.mkdir(parents=True)
    assert _run(f"git clone {opt} {_p(source)} {_p(dest)}", repo) is not None


@pytest.mark.parametrize("spelling", ["F:/dev/example-game-repo/clones/x", "F:\\dev\\example-game-repo\\clones\\x", "'F:\\dev\\x'"])
def test_drive_letter_dest_is_absolute_not_joined_onto_cwd(repo, source, spelling):
    assert _run(f"git clone {_p(source)} {spelling}", repo) is None
    assert not (repo / "F:").exists()


def test_drive_letter_classifier_is_host_independent():
    assert guard._DRIVE_ABS_RE.match("F:/dev/x") and guard._DRIVE_ABS_RE.match("f:\\dev")
    assert not guard._DRIVE_ABS_RE.match("dev/F:/x")
