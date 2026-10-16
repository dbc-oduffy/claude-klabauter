"""main_commit_refusal: main + remote refuses; everything else admits; no spawns."""

import subprocess

import pytest

from coordinator_core.git.main_commit_policy import REMEDY, main_commit_refusal


def _repo(tmp_path, head="ref: refs/heads/main\n", remote=True):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text(head)
    cfg = "[core]\n\tbare = false\n"
    if remote:
        cfg += '[remote "origin"]\n\turl = x\n'
    (git / "config").write_text(cfg)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def test_main_with_remote_refused(tmp_path):
    msg = main_commit_refusal(_repo(tmp_path), override_reason=None)
    assert msg and REMEDY in msg


def test_main_without_remote_admitted(tmp_path):
    assert main_commit_refusal(_repo(tmp_path, remote=False), override_reason=None) is None


def test_work_branch_admitted(tmp_path):
    r = _repo(tmp_path, head="ref: refs/heads/work/machine-a/x\n")
    assert main_commit_refusal(r, override_reason=None) is None


def test_detached_admitted(tmp_path):
    r = _repo(tmp_path, head="0123456789abcdef0123456789abcdef01234567\n")
    assert main_commit_refusal(r, override_reason=None) is None


def test_override_admits_blank_does_not(tmp_path):
    r = _repo(tmp_path)
    assert main_commit_refusal(r, override_reason="release index") is None
    assert main_commit_refusal(r, override_reason="  ") is not None


def test_non_string_reason_does_not_admit_or_raise(tmp_path):
    r = _repo(tmp_path)
    assert main_commit_refusal(r, override_reason=True) is not None


def test_master_trunk_not_refused(tmp_path):
    r = _repo(tmp_path, head="ref: refs/heads/master\n")
    assert main_commit_refusal(r, override_reason=None) is None


def test_worktree_uses_common_dir_config(tmp_path):
    common = tmp_path / "main" / ".git"
    wt_git = common / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (common / "config").write_text('[remote "origin"]\n\turl = x\n')
    (wt_git / "HEAD").write_text("ref: refs/heads/main\n")
    (wt_git / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    assert main_commit_refusal(wt, override_reason=None) is not None
