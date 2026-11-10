"""locked_rmw must work when the repo's .git is write-denied (sandboxed child).

Falsifier: red while lock sidecars live under .git, green once they live under
<repo>/.coordinator-local/locks/. The denial is simulated by patching the
create/open/mkdir primitives; no ACL or mode bit is ever touched.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from coordinator_core.locked_write import locked_rmw

_WRITE_FLAGS = os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC


def _repo(tmp_path: Path) -> Path:
    git = tmp_path / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return tmp_path


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _deny_git_writes(monkeypatch: pytest.MonkeyPatch, git: Path) -> None:
    """Make every mkdir/open-for-write/replace under *git* raise PermissionError(13)."""
    git_s = os.path.normcase(os.path.realpath(str(git)))

    def _under_git(path: object) -> bool:
        try:
            p = os.path.normcase(os.path.realpath(os.fsdecode(path)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return p == git_s or p.startswith(git_s + os.sep)

    def _denied(path: object) -> PermissionError:
        return PermissionError(errno.EACCES, "Permission denied", os.fsdecode(path))  # type: ignore[arg-type]

    real_open, real_mkdir, real_replace = os.open, os.mkdir, os.replace

    def fake_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS and _under_git(path):
            raise _denied(path)
        return real_open(path, flags, *args, **kwargs)

    def fake_mkdir(path, *args, **kwargs):
        if _under_git(path):
            raise _denied(path)
        return real_mkdir(path, *args, **kwargs)

    def fake_replace(src, dst, *args, **kwargs):
        if _under_git(dst):
            raise _denied(dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "mkdir", fake_mkdir)
    monkeypatch.setattr(os, "replace", fake_replace)


def test_locked_rmw_takes_lock_with_git_write_denied(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    target = repo / "state" / "child.txt"
    target.parent.mkdir(parents=True)
    target.write_text("a\n", encoding="utf-8")
    git = repo / ".git"
    before = _tree(git)
    ran: list[str] = []

    def fn(text: str) -> str:
        ran.append(text)
        return text + "b\n"

    _deny_git_writes(monkeypatch, git)
    locked_rmw(target, fn, repo_root=repo)

    assert ran == ["a\n"]
    assert target.read_text(encoding="utf-8") == "a\nb\n"
    locks = repo / ".coordinator-local" / "locks"
    assert locks.is_dir() and list(locks.glob("*.lock"))
    assert _tree(git) == before


def _legacy_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-locks"


def _target(repo: Path) -> Path:
    target = repo / "state" / "t.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a\n", encoding="utf-8")
    return target


def test_legacy_only_holder_blocks_a_dual_acquirer(tmp_path):
    from coordinator_core import locked_write as lw

    repo = _repo(tmp_path)
    target = _target(repo)
    _legacy_dir(repo).mkdir()
    fd = os.open(
        str(_legacy_dir(repo) / f"{lw._lock_key(target)}.lock"), os.O_CREAT | os.O_RDWR, 0o600
    )
    try:
        assert lw._plat_try_lock(fd)
        ran: list[str] = []
        with pytest.raises(lw.LockTimeout):
            locked_rmw(target, lambda t: ran.append(t) or t + "b\n", repo_root=repo, timeout=0.3)
        assert ran == [] and target.read_text(encoding="utf-8") == "a\n"
    finally:
        lw._plat_unlock(fd)
        os.close(fd)
    locked_rmw(target, lambda t: t + "b\n", repo_root=repo, timeout=2)
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_denied_git_dual_acquirer_takes_only_the_new_lock(tmp_path, monkeypatch):
    from coordinator_core import locked_write as lw

    repo = _repo(tmp_path)
    target = _target(repo)
    opened: list[str] = []
    real = lw._open_sidecar_bounded

    def spy(make_dir, filename, flags, timeout, **kw):
        fd = real(make_dir, filename, flags, timeout, **kw)
        opened.append(os.path.basename(os.fspath(make_dir().parent)))
        return fd

    _deny_git_writes(monkeypatch, repo / ".git")
    monkeypatch.setattr(lw, "_open_sidecar_bounded", spy)
    locked_rmw(target, lambda t: t + "b\n", repo_root=repo, timeout=2)
    assert target.read_text(encoding="utf-8") == "a\nb\n"
    assert opened == [".coordinator-local"]


def test_dual_acquire_orders_new_then_legacy_and_releases_in_reverse(tmp_path, monkeypatch):
    from coordinator_core import locked_write as lw

    repo = _repo(tmp_path)
    target = _target(repo)
    fd_dir: dict[int, str] = {}
    acquired: list[str] = []
    released: list[str] = []
    real_open, real_try, real_unlock = lw._open_sidecar_bounded, lw._plat_try_lock, lw._plat_unlock

    def spy_open(make_dir, filename, flags, timeout, **kw):
        fd = real_open(make_dir, filename, flags, timeout, **kw)
        fd_dir[fd] = make_dir().name
        return fd

    def spy_try(fd):
        ok = real_try(fd)
        if ok:
            acquired.append(fd_dir[fd])
        return ok

    def spy_unlock(fd):
        released.append(fd_dir[fd])
        real_unlock(fd)

    monkeypatch.setattr(lw, "_open_sidecar_bounded", spy_open)
    monkeypatch.setattr(lw, "_plat_try_lock", spy_try)
    monkeypatch.setattr(lw, "_plat_unlock", spy_unlock)
    locked_rmw(target, lambda t: t + "b\n", repo_root=repo, timeout=2)
    assert acquired == ["locks", "coordinator-locks"]
    assert released == ["coordinator-locks", "locks"]


def test_transient_legacy_open_denial_on_windows_is_retried_not_skipped(tmp_path, monkeypatch):
    from coordinator_core import locked_write as lw

    repo = _repo(tmp_path)
    target = _target(repo)
    monkeypatch.setattr(lw, "_FCNTL_AVAILABLE", False)
    real_open = os.open
    legacy_s = os.path.normcase(str(_legacy_dir(repo)))
    attempts: list[int] = []

    def flaky_open(path, flags, *args, **kwargs):
        if os.path.normcase(os.fspath(path)).startswith(legacy_s):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(errno.EACCES, "sharing violation", os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", flaky_open)
    opened: list[str] = []
    real_bounded = lw._open_sidecar_bounded

    def spy(make_dir, filename, flags, timeout, **kw):
        fd = real_bounded(make_dir, filename, flags, timeout, **kw)
        opened.append(make_dir().name)
        return fd

    monkeypatch.setattr(lw, "_open_sidecar_bounded", spy)
    locked_rmw(target, lambda t: t + "b\n", repo_root=repo, timeout=2)
    assert opened == ["locks", "coordinator-locks"]


def test_persistent_legacy_open_denial_on_windows_skips_after_short_bound(tmp_path, monkeypatch):
    from coordinator_core import locked_write as lw

    repo = _repo(tmp_path)
    target = _target(repo)
    monkeypatch.setattr(lw, "_FCNTL_AVAILABLE", False)
    _deny_git_writes(monkeypatch, repo / ".git")
    locked_rmw(target, lambda t: t + "b\n", repo_root=repo, timeout=2)
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_denial_simulation_refuses_a_sidecar_under_git(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _deny_git_writes(monkeypatch, repo / ".git")
    with pytest.raises(PermissionError):
        os.mkdir(repo / ".git" / "coordinator-locks")
    with pytest.raises(PermissionError):
        os.open(str(repo / ".git" / "x.lock"), os.O_CREAT | os.O_WRONLY)
    (repo / "state").mkdir()
