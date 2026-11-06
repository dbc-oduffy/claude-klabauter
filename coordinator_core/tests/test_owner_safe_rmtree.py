import os
import shutil
import stat
import sys

import pytest

from coordinator_core import owner_safe_rmtree as m

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")


@posix_only
def test_make_removable_dir_gains_rwx_keeps_other_bits(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o200)
    m.make_removable(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    os.chmod(d, 0o250)
    m.make_removable(d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o750


@posix_only
def test_make_removable_file_gains_write_keeps_read(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    os.chmod(f, 0o444)
    m.make_removable(f)
    assert stat.S_IMODE(f.stat().st_mode) == 0o644


@posix_only
def test_make_removable_never_leaves_dir_write_only(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o500)
    m.make_removable(d)
    mode = stat.S_IMODE(d.stat().st_mode)
    assert mode & 0o700 == 0o700


@posix_only
def test_make_removable_on_symlink_leaves_target_mode(tmp_path):
    target = tmp_path / "secret"
    target.write_text("x")
    os.chmod(target, 0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    m.make_removable(link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_make_removable_missing_path_raises(tmp_path):
    with pytest.raises(OSError):
        m.make_removable(tmp_path / "nope")


@posix_only
def test_rmtree_removes_readonly_tree_via_kwargs(tmp_path):
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
def test_retry_handler_leaves_dir_traversable_when_retry_fails(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o000)

    def failing(_p):
        raise OSError("ENOTEMPTY")

    with pytest.raises(OSError):
        m.rmtree_retry(failing, str(d), None)
    assert stat.S_IMODE(d.stat().st_mode) & 0o700 == 0o700


def test_rmtree_kwargs_key_matches_python_version():
    key = next(iter(m.rmtree_kwargs()))
    assert key == ("onexc" if sys.version_info >= (3, 12) else "onerror")
