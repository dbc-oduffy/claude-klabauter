"""
Tests for coordinator_core.install.junction (C1).

Every test uses `tmp_path` — never the real fleet env. Windows-specific
assertions are gated on `os.name == "nt"` (platform gating); AC2's
dependency-pin test is the one exception that must fail loudly rather than
skip on absence — see its own docstring.
"""

from __future__ import annotations

import os
import shutil
import stat

import pytest

from coordinator_core.install import junction


def _make_target(tmp_path, name="target", payload="hello"):
    target = tmp_path / name
    target.mkdir()
    (target / "f.txt").write_text(payload, encoding="utf-8", newline="\n")
    return target


def test_round_trip_create_detect_target_remove(tmp_path):
    target = _make_target(tmp_path)
    link = tmp_path / "link"

    junction.create_junction(link, target)

    assert junction.is_junction(link)
    assert (link / "f.txt").read_text(encoding="utf-8") == "hello"
    assert junction.junction_target(link).resolve() == target.resolve()

    junction.remove_junction(link)

    assert not link.exists()
    assert target.exists()
    assert (target / "f.txt").read_text(encoding="utf-8") == "hello"


def test_is_junction_false_for_real_directory(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    assert junction.is_junction(real_dir) is False


def test_is_junction_false_for_plain_file(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("x", encoding="utf-8", newline="\n")
    assert junction.is_junction(f) is False


def test_is_junction_false_for_nonexistent_path(tmp_path):
    assert junction.is_junction(tmp_path / "does-not-exist") is False


def test_junction_target_none_for_non_junction(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    assert junction.junction_target(real_dir) is None
    assert junction.junction_target(tmp_path / "missing") is None


def test_remove_junction_leaves_target_payload_intact(tmp_path):
    target = _make_target(tmp_path)
    link = tmp_path / "link"
    junction.create_junction(link, target)

    junction.remove_junction(link)

    assert target.is_dir()
    assert (target / "f.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.skipif(os.name != "nt", reason="junction semantics are nt-specific")
def test_islink_trap_reads_false_on_a_junction(tmp_path):
    target = _make_target(tmp_path)
    link = tmp_path / "link"
    junction.create_junction(link, target)

    assert os.path.islink(link) is False
    assert link.is_symlink() is False
    assert os.path.isdir(link) is True


@pytest.mark.skipif(os.name != "nt", reason="junction semantics are nt-specific")
def test_shutil_rmtree_refuses_on_a_junction(tmp_path):
    target = _make_target(tmp_path)
    link = tmp_path / "link"
    junction.create_junction(link, target)

    with pytest.raises(OSError):
        shutil.rmtree(link)

    assert target.exists()
    assert (target / "f.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="junction semantics are nt-specific")
def test_reparse_tag_discriminator_matches_mount_point(tmp_path):
    target = _make_target(tmp_path)
    link = tmp_path / "link"
    junction.create_junction(link, target)

    st = os.lstat(link)
    assert st.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT


@pytest.mark.skipif(os.name != "nt", reason="AC2 pins an nt-only private API")
def test_create_junction_dependency_is_present_and_callable():
    """AC2: fails loudly, never skips, if _winapi.CreateJunction disappears.

    A Python upgrade that drops this private API must show up here as a red
    test, not as a silent fallback to the broken directory-rename path at
    fleet-rebuild time (see junction.py's module docstring).
    """
    import _winapi

    assert hasattr(_winapi, "CreateJunction"), (
        "_winapi.CreateJunction is absent from this stdlib build — "
        "junction.create_junction refuses rather than falling back to a "
        "directory rename; this test must fail, not skip, when that happens"
    )
    assert callable(_winapi.CreateJunction)


def test_create_junction_argument_order_is_target_then_link(tmp_path):
    """Locks the verified _winapi.CreateJunction(target, link) order via the
    public wrapper's observable behavior, so a swapped-argument regression
    in create_junction shows up as a wrong resolved target, not a crash.
    """
    target = _make_target(tmp_path, name="real-target", payload="order-check")
    link = tmp_path / "order-link"

    junction.create_junction(link, target)

    resolved = junction.junction_target(link)
    assert resolved is not None
    assert resolved.resolve() == target.resolve()
    assert not (tmp_path / "real-target-does-not-exist").exists()
