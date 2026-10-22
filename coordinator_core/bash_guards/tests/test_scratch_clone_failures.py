"""scratch_clone_failures with an injected git runner: UNREADABLE on rc != 0 or a raised OSError,
[] for a clean repo."""

from __future__ import annotations

from coordinator_core.bash_guards._unborn_scratch_repo import UNREADABLE, scratch_clone_failures


def _scratch_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    clone = tmp_path / "scratch" / "c"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return str(clone)


def test_nonzero_rc_is_unreadable(tmp_path):
    root = _scratch_repo(tmp_path)
    assert scratch_clone_failures(root, lambda args, cwd: (128, "")) == [UNREADABLE]


def test_raised_oserror_is_unreadable(tmp_path):
    root = _scratch_repo(tmp_path)

    def boom(args, cwd):
        raise OSError("spawn failed")

    assert scratch_clone_failures(root, boom) == [UNREADABLE]


def test_clean_repo_has_no_failures(tmp_path):
    root = _scratch_repo(tmp_path)
    assert scratch_clone_failures(root, lambda args, cwd: (0, "")) == []
