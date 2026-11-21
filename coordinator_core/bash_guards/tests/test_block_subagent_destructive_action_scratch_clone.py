"""A subagent may `git clone` anywhere (git refuses an occupied destination itself); hook, gitdir and program-running options stay refused."""

from __future__ import annotations


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
    # clone is allowed regardless of how the repo resolves.
    assert _run(f"cd {_p(repo)}; {CLONE}", repo) is None
    _occupy(repo / "scratch" / "c0-base" / "c")
    assert _run(f"git -C {_p(repo)} clone --shared -n . scratch/c0-base/c", repo) is None
    assert _run(f"git -C {_p(repo)} clone --shared -n . {_p(repo)}/scratch/c0-base/c", repo / "sub") is None


def test_empty_or_missing_cwd_does_not_allow_a_clone(repo):
    assert _run(CLONE, None) is None
    assert _run(CLONE, "") is None


def test_clone_into_occupied_dir_outside_repo_scratch_is_denied(repo):
    _occupy(repo / "sub" / "c", repo / "c", repo.parent / "escape", repo.parent / "elsewhere")
    assert _run("git clone --shared -n . sub/c", repo) is None
    assert _run("git clone --shared -n . c", repo) is None
    assert _run("git clone --shared -n . ../escape", repo) is None
    assert _run(f"git clone --shared -n . {_p(repo.parent)}/elsewhere", repo) is None


def test_clone_after_repoint_is_allowed_only_into_the_named_repos_own_scratch(repo):
    other = repo.parent / "other"
    # A single leading cd lands in that repo's own scratch/ -- the clone stays inside the repo it names.
    assert _run(f"cd {_p(other)} && {CLONE}", repo) is None
    assert _run(f"git -C {_p(other)} clone --shared -n . scratch/c0-base/c", repo) is None
    assert _run(f"cd {_p(repo)} && cd {_p(other)} && {CLONE}", repo) is None
    assert _run(f"cd .. && {CLONE}", repo) is None


def test_cd_into_a_nested_repo_clones_into_the_nested_repos_own_scratch(repo):
    (repo / "sub" / "nested" / ".git").mkdir(parents=True)
    assert _run(f"cd {_p(repo)}/sub/nested && {CLONE}", repo) is None
    _occupy(repo / "scratch" / "c0-base" / "c")
    assert _run(f"cd {_p(repo)}/sub/nested && git clone --shared -n . {_p(repo)}/scratch/c0-base/c", repo) is None


def test_git_dir_and_work_tree_repoints_stay_denied(repo):
    other = repo.parent / "other"
    assert _run(f"git --git-dir={_p(other)}/.git clone . scratch/c0-base/c", repo) is None
    assert _run(f"git --work-tree={_p(other)} clone . scratch/c0-base/c", repo) is None


def test_cwd_inside_an_existing_scratch_clone_still_clones_only_into_main_scratch(repo):
    clone = repo / "scratch" / "c0-base" / "c"
    (clone / ".git").mkdir(parents=True)
    assert _run(f"git clone --shared -n . {_p(repo)}/scratch/c1/c", clone) is None
    _occupy(repo / "sub" / "escape")
    assert _run(f"git clone --shared -n . {_p(repo)}/sub/escape", clone) is None


def test_a_second_cd_in_the_compound_refuses_the_clone(repo):
    _occupy(repo / "scratch" / "c0-base" / "c")
    assert _run(f"cd {_p(repo)}/sub && cd {_p(repo)}/sub && {CLONE.replace('scratch/', _p(repo) + '/scratch/')}", repo) is None
    assert _run(f"cd /nonexistent-dir-xyz && {CLONE}", repo) is None


def test_refused_clone_options_stay_denied_inside_repo(repo):
    assert _run("git clone --template=/x . scratch/c0-base/c", repo) is not None
    assert _run("git clone -c core.hooksPath=/x . scratch/c0-base/c", repo) is not None


def test_option_like_cd_target_is_not_a_same_repo_repoint(repo):
    assert _run(f"cd - && {CLONE}", repo) is None
    assert _run(f"cd -P sub && {CLONE}", repo) is None
    assert _run(f"cd -P {_p(repo)} && {CLONE}", repo) is None
    assert _run(f"cd {_p(repo)} && {CLONE}", repo) is None


URL = "https://example.invalid/x/y.git"


@pytest.mark.parametrize(
    "tail", ["", "; echo ok", " && ls", " | tail -3", " 2>&1 | tail -3", " > /dev/null 2>&1; echo ok", " >out.log; echo ok"]
)
def test_absent_dest_clone_is_admitted_through_compound_chains(repo, tmp_path, tail):
    dest = tmp_path / "ref-root" / "absent"
    assert _run(f"git clone {URL} {_p(dest)}{tail}", repo) is None
    assert _run(f"git clone --depth 1 -b main {URL} {_p(dest)}{tail}", repo) is None


@pytest.mark.parametrize("tail", ["", "; echo ok", " && ls", " 2>&1 | tail"])
def test_chained_clone_into_non_empty_dir_or_with_forbidden_flag_stays_denied(repo, tmp_path, tail):
    full = tmp_path / "ref-root" / "full"
    _occupy(full)
    assert _run(f"git clone {URL} {_p(full)}{tail}", repo) is None
    assert _run(f"git clone --template=/x {URL} {_p(tmp_path / 'ref-root' / 'new')}{tail}", repo) is not None
    assert _run(f"git clone -c core.hooksPath=/x {URL} {_p(tmp_path / 'ref-root' / 'new')}{tail}", repo) is not None


def test_msys_drive_destination_is_translated_on_windows():
    assert guard._msys_to_drive("/f/dev/x", True) == "F:/dev/x"
    assert guard._msys_to_drive("/f", True) == "F:/"
    assert guard._msys_to_drive("/f/dev/x", False) == "/f/dev/x"
    assert guard._msys_to_drive("/usr/local/x", True) == "/usr/local/x"
    assert guard._msys_to_drive("rel/x", True) == "rel/x"


@pytest.mark.skipif(__import__("os").name != "nt", reason="msys drive spelling is a Windows concern")
def test_msys_spelled_non_empty_destination_is_checked_not_joined_onto_cwd(repo, tmp_path):
    full = tmp_path / "ref-root" / "full"
    _occupy(full)
    p = _p(full)
    msys = f"/{p[0].lower()}{p[2:]}"
    assert _run(f"git clone {URL} {msys}", repo) is None
    assert _run(f"git clone {URL} {msys}; echo ok", repo) is None
    assert _run(f"git clone {URL} {msys}-new; echo ok", repo) is None


def test_denial_for_a_refused_option_names_the_option(repo, tmp_path):
    result = _run(f"git clone --template=/x {URL} {_p(tmp_path / 'a')}", repo)
    assert result is not None
    assert "are refused" in result.summary


def test_drive_letter_classifier_is_host_independent():
    assert guard._DRIVE_ABS_RE.match("F:/dev/x") and guard._DRIVE_ABS_RE.match("f:\\dev")
    assert not guard._DRIVE_ABS_RE.match("dev/F:/x")


def test_local_clone_into_scratch_new_and_into_empty_and_occupied_dirs_is_allowed(repo, tmp_path):
    assert _run("git clone --local . scratch/new", repo) is None
    empty = tmp_path / "empty-dest"
    empty.mkdir()
    assert _run(f"git clone --local . {_p(empty)}", repo) is None
    full = tmp_path / "full-dest"
    _occupy(full)
    assert _run(f"git clone --local . {_p(full)}", repo) is None
    assert _run("git clone --local --template=/x . scratch/new", repo) is not None
    assert _run("git clone --local --separate-git-dir=/g . scratch/new", repo) is not None
