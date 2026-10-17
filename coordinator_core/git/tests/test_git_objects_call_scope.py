"""Call-count pins for git_objects under call_io_scope: config read-through and HEAD-memo invalidation."""

from __future__ import annotations

import builtins
from pathlib import Path

from coordinator_core.git import call_io_scope, git_objects

SHA_A = "a" * 40
SHA_B = "b" * 40


def _repo(tmp_path: Path) -> Path:
    gitdir = tmp_path / ".git"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    (gitdir / "refs" / "heads" / "main").write_text(SHA_A + "\n")
    (gitdir / "config").write_text("[core]\n\tlogallrefupdates = true\n")
    return gitdir


def _count_config_opens(monkeypatch, gitdir: Path) -> list:
    opened: list = []
    real_open = builtins.open
    target = str(gitdir / "config")

    def counting(file, *a, **kw):
        if isinstance(file, (str, Path)) and str(file) == target:
            opened.append(file)
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting)
    return opened


def test_config_opened_once_per_scope(tmp_path, monkeypatch):
    gitdir = _repo(tmp_path)
    opened = _count_config_opens(monkeypatch, gitdir)
    with call_io_scope.call_io_scope():
        for _ in range(5):
            git_objects.append_reflog(gitdir, "refs/heads/main", SHA_A, SHA_B, "c <c@x> 1 +0000", "m")
    assert len(opened) == 1


def test_config_opened_every_call_outside_scope(tmp_path, monkeypatch):
    gitdir = _repo(tmp_path)
    opened = _count_config_opens(monkeypatch, gitdir)
    for _ in range(3):
        git_objects.append_reflog(gitdir, "refs/heads/main", SHA_A, SHA_B, "c <c@x> 1 +0000", "m")
    assert len(opened) == 3


def test_logallrefupdates_false_respected_through_scope(tmp_path):
    gitdir = _repo(tmp_path)
    (gitdir / "config").write_text("[core]\n\tlogallrefupdates = false\n")
    with call_io_scope.call_io_scope():
        assert git_objects._log_all_ref_updates(gitdir) is False


def _populate() -> None:
    call_io_scope.memo(call_io_scope.HEAD_NAMESPACE, "k", lambda: "stale")


def _populated() -> bool:
    return call_io_scope.memo(call_io_scope.HEAD_NAMESPACE, "k", lambda: "fresh") == "stale"


def test_head_memo_cleared_after_successful_cas_ref(tmp_path):
    gitdir = _repo(tmp_path)
    with call_io_scope.call_io_scope():
        _populate()
        assert _populated()
        assert git_objects.cas_ref(gitdir, "refs/heads/main", SHA_A, SHA_B) is True
        assert not _populated()


def test_head_memo_cleared_after_lost_cas_ref(tmp_path):
    gitdir = _repo(tmp_path)
    with call_io_scope.call_io_scope():
        _populate()
        assert git_objects.cas_ref(gitdir, "refs/heads/main", SHA_B, SHA_A) is False
        assert not _populated()


def test_head_memo_cleared_after_cas_head_symref(tmp_path):
    gitdir = _repo(tmp_path)
    with call_io_scope.call_io_scope():
        _populate()
        assert git_objects.cas_head_symref(gitdir, "refs/heads/main", "refs/heads/other") is True
        assert not _populated()


def test_head_memo_cleared_after_lost_cas_head_symref(tmp_path):
    gitdir = _repo(tmp_path)
    with call_io_scope.call_io_scope():
        _populate()
        assert git_objects.cas_head_symref(gitdir, "refs/heads/nope", "refs/heads/other") is False
        assert not _populated()
