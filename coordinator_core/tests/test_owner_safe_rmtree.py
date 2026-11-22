import os
import shutil
import stat
import sys

import pytest

from coordinator_core import owner_safe_rmtree as m
from pathlib import Path

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")


@posix_only
def test_make_removable_dir_gains_rwx_keeps_other_bits(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o200)
    m.make_removable(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    os.chmod(d, 0o250)
    m.make_removable(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o750


@posix_only
def test_make_removable_file_gains_write_keeps_read(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    os.chmod(f, 0o444)
    m.make_removable(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o644


@posix_only
def test_make_removable_never_leaves_dir_write_only(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o500)
    m.make_removable(d)
    mode = stat.S_IMODE(d.stat().st_mode)
    assert mode & 0o700 == 0o700


@posix_only
def test_make_removable_on_symlink_leaves_target_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_text("x")
    os.chmod(target, 0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    m.make_removable(link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_make_removable_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        m.make_removable(tmp_path / "nope")


@posix_only
def test_rmtree_removes_readonly_tree_via_kwargs(tmp_path: Path) -> None:
    root = tmp_path / "t"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "f").write_text("x")
    os.chmod(sub / "f", 0o400)
    os.chmod(sub, 0o500)
    os.chmod(root / "a", 0o500)
    shutil.rmtree(root, **m.rmtree_kwargs())
    assert not root.exists()


@posix_only
def test_retry_handler_leaves_dir_traversable_when_retry_fails(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o000)

    def failing(_p):
        raise OSError("ENOTEMPTY")

    with pytest.raises(OSError):
        m.rmtree_retry(failing, str(d), None)
    assert stat.S_IMODE(d.stat().st_mode) & 0o700 == 0o700


def _readonly_tree(root: Path, files: int, per_dir: int = 25) -> None:
    for n in range(files):
        d = root / f"d{n // per_dir}" / "sub"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"f{n}.txt"
        f.write_text("x")
        os.chmod(f, 0o444)


def test_fast_rmtree_removes_readonly_multi_dir_file_tree(tmp_path: Path) -> None:
    root = tmp_path / "big"
    _readonly_tree(root, 600)
    assert m.fast_rmtree(root) is True
    assert not root.exists()


@posix_only
def test_fast_rmtree_removes_unwritable_dirs_on_posix(tmp_path: Path) -> None:
    root = tmp_path / "t"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "f").write_text("x")
    os.chmod(sub / "f", 0o400)
    os.chmod(sub, 0o500)
    os.chmod(root / "a", 0o500)
    assert m.fast_rmtree(root) is True
    assert not root.exists()


def test_fast_rmtree_missing_path_is_gone_without_raising(tmp_path: Path) -> None:
    assert m.fast_rmtree(tmp_path / "nope") is True


def test_fast_rmtree_removes_a_single_file(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("x")
    os.chmod(f, 0o444)
    assert m.fast_rmtree(f) is True
    assert not f.exists()


def test_fast_rmtree_does_not_follow_a_directory_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    root = tmp_path / "t"
    root.mkdir()
    link = root / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege not held")
    assert m.fast_rmtree(root) is True
    assert not root.exists()
    assert (target / "keep.txt").read_text() == "x"


def test_fast_rmtree_held_handle_returns_false_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "t"
    (root / "d").mkdir(parents=True)
    held = root / "d" / "held.txt"
    held.write_text("x")
    (root / "d" / "free.txt").write_text("x")
    real_unlink = os.unlink

    def unlink(p, *a, **k):
        if os.path.basename(os.fspath(p)) == "held.txt":
            raise PermissionError("held")
        return real_unlink(p, *a, **k)

    monkeypatch.setattr(os, "unlink", unlink)
    assert m.fast_rmtree(root) is False
    assert held.exists()
    assert not (root / "d" / "free.txt").exists()
    monkeypatch.undo()
    assert m.fast_rmtree(root) is True
    assert not root.exists()


def test_fast_rmtree_issues_one_unlink_per_readonly_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "t"
    _readonly_tree(root, 40)
    calls: list[str] = []
    real_unlink = os.unlink

    def counting(p, *a, **k):
        calls.append(os.fspath(p))
        return real_unlink(p, *a, **k)

    monkeypatch.setattr(os, "unlink", counting)
    assert m.fast_rmtree(root) is True
    assert len(calls) == 40


def test_rmtree_kwargs_key_matches_python_version() -> None:
    key = next(iter(m.rmtree_kwargs()))
    assert key == ("onexc" if sys.version_info >= (3, 12) else "onerror")
