"""Tests for coordinator_core.git.repo_root.

Spec backlink: docs/plans/2026-08-06-eliminate-claude-klabauter-s-non-test-subprocess-
    spawn-population.md, chunk C1.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import repo_root


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

    # `git_dir()` must return the worktree-PRIVATE gitdir (mirrors
    # `--git-dir`), not the shared common dir `git_common_dir()` resolves to
    # -- see module docstring's `git_dir()` vs `git_common_dir()` bullets.
    assert os.path.normpath(repo_root.git_dir(str(sub))) == os.path.normpath(str(private_gitdir))
    assert repo_root.git_dir(str(sub)) != repo_root.git_common_dir(str(sub))


def test_not_in_a_repo_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lone = tmp_path / "lonely"
    lone.mkdir()
    assert repo_root.show_toplevel(str(lone)) is None


def test_git_missing_oserror_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lone = tmp_path / "lonely2"
    lone.mkdir()
    assert repo_root.show_toplevel(str(lone)) is None
    assert repo_root.is_inside_work_tree(str(lone)) is False


def test_timeout_returns_none(tmp_path, monkeypatch):
    def _fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lone = tmp_path / "lonely3"
    lone.mkdir()
    assert repo_root.show_prefix(str(lone)) is None


def test_memo_memoizes_same_cwd_spawn_fallback(tmp_path, monkeypatch):
    lone = tmp_path / "no_dot_git"
    lone.mkdir()
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    repo_root.show_toplevel(str(lone))
    repo_root.show_toplevel(str(lone))
    assert len(calls) == 1


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
    assert repo_root.show_toplevel(str(lone)) is None
    assert repo_root.is_inside_work_tree(str(lone)) is False


def test_failed_walk_resolution_is_not_memoized(tmp_path):
    lone = tmp_path / "becomes-a-repo"
    lone.mkdir()

    assert repo_root.show_toplevel(str(lone)) is None

    (lone / ".git").mkdir()
    assert repo_root.show_toplevel(str(lone)) == str(lone)


def test_failed_spawn_resolution_is_not_memoized(tmp_path, monkeypatch):
    lone = tmp_path / "no_dot_git_yet"
    lone.mkdir()
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repo")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert repo_root.show_toplevel(str(lone)) is None
    assert repo_root.show_toplevel(str(lone)) is None
    assert len(calls) == 2


def test_positive_spawn_result_still_memoized(tmp_path, monkeypatch):
    lone = tmp_path / "no_dot_git_positive"
    lone.mkdir()
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=str(lone) + "\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    first = repo_root.show_toplevel(str(lone))
    second = repo_root.show_toplevel(str(lone))
    assert first == str(lone)
    assert second == str(lone)
    assert len(calls) == 1


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
    # Review: pins the blast-radius claim itself (P3a) -- that
    # `--show-toplevel`/`--git-dir`/`--git-common-dir`/`--absolute-git-dir`/
    # `--is-inside-work-tree` are the ONLY forms this module's fix leaves
    # genuinely untouched, because real `git` never returns rc=0 with empty
    # stdout for them the way it legitimately does for `--show-prefix` at the
    # toplevel. Runs real `git rev-parse` directly (bypassing this module's
    # walk, which would short-circuit before ever reaching the spawn fallback
    # this claim is about) so a future git version -- or a mistaken
    # assumption -- that started emitting empty stdout on success for one of
    # these forms would fail this test instead of silently regressing the
    # "safe to change" premise `show_prefix()`'s docstring rests on.
    #
    # Review: code-reviewer (F9) -- the original version of this test covered
    # only the three walk-backed forms, omitting `--absolute-git-dir` and
    # `--is-inside-work-tree`, the two forms that spawn on EVERY call (not
    # just as a walk fallback) and are therefore the most exposed to the
    # empty-stdout distinction this test exists to pin. Both added below.
    repo = tmp_path / "blast_radius_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True
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
    )
    assert prefix_result.returncode == 0
    assert prefix_result.stdout.strip() == ""


def test_git_common_dir_empty_on_success_does_not_return_resolved_cwd(tmp_path, monkeypatch):
    # Review: code-reviewer (F8, P2) -- P3(a)'s originally requested test:
    # fake rc=0 with empty stdout for `--git-common-dir` and assert
    # `git_common_dir()` does NOT return the resolved cwd. Unlike
    # test_real_git_never_emits_empty_stdout_on_success_except_show_prefix
    # (which shells `git rev-parse` directly and pins a property of real
    # git, never calling into this module at all), this test calls
    # `repo_root.git_common_dir()` itself, forcing the walk to miss (no
    # `.git` entry) so the mocked spawn fallback actually executes. It
    # closes the regression surface the original finding named: a future
    # change that removed the `spawned == ""` guard in `git_common_dir()`
    # would flip this test's result from `None` back to the caller's cwd,
    # and this test would catch it. FAILS if that guard is removed.
    lone = tmp_path / "not_a_repo"
    lone.mkdir()

    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = repo_root.git_common_dir(str(lone))
    assert result is None
    assert result != str(lone)


def test_show_prefix_at_real_git_toplevel_returns_empty_string(tmp_path):
    # Review: the four tests above this one all mock `subprocess.run`
    # against a synthetic `.git` directory -- nothing exercises real `git`
    # at a real toplevel, which is the exact scenario this module's fix
    # exists for. This test runs `git init` and calls into the seam
    # unmocked so the suite verifies the mock's faithfulness itself.
    repo = tmp_path / "real_repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(repo),
        check=True,
        capture_output=True,
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
    repo_root.show_toplevel(str(lone))
    assert seen_kwargs["timeout"] == 2.0
    assert seen_kwargs["stdin"] == subprocess.DEVNULL
    assert seen_kwargs["capture_output"] is True
    assert seen_kwargs["text"] is True
    assert "creationflags" in seen_kwargs
