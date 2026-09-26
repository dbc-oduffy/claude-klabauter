
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import repo_root
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture(autouse=True)
def _clear_memo():
    repo_root.clear_memo()
    yield
    repo_root.clear_memo()


def _make_repo(tmp_path, name="repo"):
    d = tmp_path / name
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    sub = d / "sub" / "dir"
    sub.mkdir(parents=True)
    return d, sub


def test_show_toplevel_walk_finds_dot_git_dir(tmp_path):
    repo, sub = _make_repo(tmp_path)
    assert repo_root.show_toplevel(str(sub)) == str(repo)


def test_git_dir_via_walk(tmp_path):
    repo, sub = _make_repo(tmp_path)
    assert repo_root.git_dir(str(sub)) == str(repo / ".git")


def test_git_common_dir_via_walk_plain_clone(tmp_path):
    repo, sub = _make_repo(tmp_path)
    assert repo_root.git_common_dir(str(sub)) == str(repo / ".git")


def test_git_common_dir_via_worktree_indirection(tmp_path):
    repo, sub = _make_repo(tmp_path, name="linked")
    private_gitdir = tmp_path / "main" / ".git" / "worktrees" / "linked"
    private_gitdir.mkdir(parents=True)
    common = tmp_path / "main" / ".git"
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    import shutil

    shutil.rmtree(repo / ".git")
    (repo / ".git").write_text(
        f"gitdir: {private_gitdir}\n", encoding="utf-8"
    )
    import os

    assert os.path.normpath(repo_root.git_common_dir(str(sub))) == os.path.normpath(str(common))


def test_git_dir_via_worktree_indirection_returns_private_not_common(tmp_path):
    repo, sub = _make_repo(tmp_path, name="linked")
    private_gitdir = tmp_path / "main" / ".git" / "worktrees" / "linked"
    private_gitdir.mkdir(parents=True)
    (private_gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    import shutil

    shutil.rmtree(repo / ".git")
    (repo / ".git").write_text(
        f"gitdir: {private_gitdir}\n", encoding="utf-8"
    )
    import os

    assert os.path.normpath(repo_root.git_dir(str(sub))) == os.path.normpath(str(private_gitdir))
    assert repo_root.git_dir(str(sub)) != repo_root.git_common_dir(str(sub))


def test_not_in_a_repo_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lone = tmp_path / "lonely"
    lone.mkdir()
    assert repo_root.git_dir(str(lone)) is None


def test_git_missing_oserror_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lone = tmp_path / "lonely2"
    lone.mkdir()
    assert repo_root.git_dir(str(lone)) is None
    assert repo_root.is_inside_work_tree(str(lone)) is False


def test_timeout_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lone = tmp_path / "lonely3"
    lone.mkdir()
    assert repo_root.show_prefix(str(lone)) is None


def test_show_toplevel_never_spawns_even_when_the_walk_finds_nothing(
    tmp_path, monkeypatch
):
    lone = tmp_path / "no_dot_git_anywhere"
    lone.mkdir()

    def _fail(*_a, **_kw):
        raise AssertionError(
            "show_toplevel spawned; it is walk-only by negative-spec -- see "
            "its docstring before reintroducing a fallback"
        )

    monkeypatch.setattr(subprocess, "run", _fail)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated.git"))
    assert repo_root.show_toplevel(str(lone)) is None


def test_bare_repo_resolves_git_dir_and_common_dir_without_spawning(
    tmp_path, monkeypatch
):
    bare = tmp_path / "bare.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    def _fail(*_a, **_kw):
        raise AssertionError("bare-repo resolution spawned; it is walk-only")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert repo_root.git_dir(str(bare)) == str(bare)
    assert repo_root.git_common_dir(str(bare)) == str(bare)
    assert repo_root.absolute_git_dir(str(bare)) == str(bare)


def test_bare_repo_has_no_toplevel(tmp_path, monkeypatch):
    bare = tmp_path / "bare2.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    def _fail(*_a, **_kw):
        raise AssertionError("show_toplevel spawned")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert repo_root.show_toplevel(str(bare)) is None


def test_cwd_inside_a_bare_repo_climbs_to_the_bare_root(tmp_path, monkeypatch):
    bare = tmp_path / "bare3.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs" / "heads").mkdir(parents=True)
    (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    def _fail(*_a, **_kw):
        raise AssertionError("bare-repo resolution spawned")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert repo_root.git_dir(str(bare / "refs" / "heads")) == str(bare)


def test_worktree_wins_over_bare_markers_at_the_same_level(tmp_path, monkeypatch):
    repo = tmp_path / "both"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "objects").mkdir()
    (repo / "refs").mkdir()
    (repo / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    def _fail(*_a, **_kw):
        raise AssertionError("resolution spawned")

    monkeypatch.setattr(subprocess, "run", _fail)
    assert repo_root.show_toplevel(str(repo)) == str(repo)
    assert repo_root.git_dir(str(repo)) == str(repo / ".git")


def test_memo_memoizes_is_inside_work_tree_spawn(tmp_path, monkeypatch):
    repo, sub = _make_repo(tmp_path)
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert repo_root.is_inside_work_tree(str(sub)) is True
    assert repo_root.is_inside_work_tree(str(sub)) is True
    assert len(calls) == 1


def test_different_cwds_do_not_share_memo_entry(tmp_path):
    repo_a, sub_a = _make_repo(tmp_path, name="repo_a")
    repo_b, sub_b = _make_repo(tmp_path, name="repo_b")

    assert repo_root.show_toplevel(str(sub_a)) == str(repo_a)
    assert repo_root.show_toplevel(str(sub_b)) == str(repo_b)
    assert repo_root.show_toplevel(str(sub_a)) == str(repo_a)


def test_is_inside_work_tree_never_derived_from_toplevel_truthiness(tmp_path, monkeypatch):
    lone = tmp_path / "some_dir"
    lone.mkdir()

    def _fake_run(cmd, **kw):
        if "--is-inside-work-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert repo_root.git_dir(str(lone)) is None
    assert repo_root.is_inside_work_tree(str(lone)) is False


def test_failed_walk_resolution_is_not_memoized(tmp_path):
    lone = tmp_path / "becomes-a-repo"
    lone.mkdir()

    assert repo_root.git_dir(str(lone)) is None

    (lone / ".git").mkdir()
    assert repo_root.show_toplevel(str(lone)) == str(lone)


def test_cwd_none_resolves_and_keys_on_current_absolute_cwd(tmp_path, monkeypatch):
    repo, sub = _make_repo(tmp_path)
    monkeypatch.chdir(sub)
    assert repo_root.show_toplevel(None) == str(repo)
    assert repo_root.show_toplevel(str(sub)) == str(repo)


def test_show_prefix_at_toplevel_returns_empty_string_not_none(tmp_path, monkeypatch):
    repo, _sub = _make_repo(tmp_path)

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = repo_root.show_prefix(str(repo))
    assert result == ""
    assert result is not None


def test_show_prefix_genuine_failure_returns_none(tmp_path, monkeypatch):
    repo, _sub = _make_repo(tmp_path)

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert repo_root.show_prefix(str(repo)) is None


def test_show_prefix_successful_empty_result_is_memoized(tmp_path, monkeypatch):
    repo, _sub = _make_repo(tmp_path)
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    first = repo_root.show_prefix(str(repo))
    second = repo_root.show_prefix(str(repo))
    assert first == ""
    assert second == ""
    assert len(calls) == 1


def test_show_prefix_failure_is_not_memoized(tmp_path, monkeypatch):
    repo, _sub = _make_repo(tmp_path)
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert repo_root.show_prefix(str(repo)) is None
    assert repo_root.show_prefix(str(repo)) is None
    assert len(calls) == 2


def test_real_git_never_emits_empty_stdout_on_success_except_show_prefix(tmp_path):
    repo = tmp_path / "blast_radius_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True,
        **no_console_creationflags(),
    )
    for form in (
        "--show-toplevel",
        "--git-dir",
        "--git-common-dir",
        "--absolute-git-dir",
        "--is-inside-work-tree",
    ):
        result = subprocess.run(
            ["git", "rev-parse", form],
            cwd=str(repo),
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        assert result.returncode == 0
        assert result.stdout.strip() != "", (
            f"{form} emitted empty stdout on success -- the blast-radius "
            "claim in repo_root.py's module docstring no longer holds"
        )
    prefix_result = subprocess.run(
        ["git", "rev-parse", "--show-prefix"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert prefix_result.returncode == 0
    assert prefix_result.stdout.strip() == ""


def test_show_toplevel_spawn_fallback_matches_path_format_absolute(tmp_path):
    repo = tmp_path / "path_format_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    plain = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    absolute = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
        cwd=str(repo), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert plain.returncode == 0 and absolute.returncode == 0
    assert plain.stdout.strip() == absolute.stdout.strip()
    assert Path(plain.stdout.strip()).is_absolute()
    assert Path(repo_root.show_toplevel(str(repo))).resolve() == Path(plain.stdout.strip()).resolve()


def test_git_common_dir_empty_on_success_does_not_return_resolved_cwd(tmp_path, monkeypatch):
    lone = tmp_path / "not_a_repo"
    lone.mkdir()

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = repo_root.git_common_dir(str(lone))
    assert result is None
    assert result != str(lone)


def test_show_prefix_at_real_git_toplevel_returns_empty_string(tmp_path):
    repo = tmp_path / "real_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )
    sub = repo / "sub"
    sub.mkdir()

    assert repo_root.show_prefix(str(repo)) == ""
    assert repo_root.show_prefix(str(sub)) == "sub/"


def test_no_console_creationflags_and_timeout_passed(tmp_path, monkeypatch):
    lone = tmp_path / "flagcheck"
    lone.mkdir()
    seen_kwargs = {}

    def _fake_run(cmd, **kw):
        seen_kwargs.update(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="/x\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    repo_root.show_prefix(str(lone))
    assert seen_kwargs["timeout"] == 2.0
    assert seen_kwargs["stdin"] == subprocess.DEVNULL
    assert seen_kwargs["capture_output"] is True
    assert seen_kwargs["text"] is True
    assert "creationflags" in seen_kwargs
