
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, door_credential


@pytest.fixture()
def isolated_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the whole warm runtime base at `tmp_path`.

    `RUNTIME_BASE_ENV` is the seam `breadcrumb._runtime_base` documents for
    exactly this: without it every run of this suite would mint a real
    directory under the operator's `%LOCALAPPDATA%` that nothing removes.
    """
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path))
    warm = tmp_path / "coordinator" / "warm"
    warm.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(warm, 0o700)
    return warm


def test_secret_lives_per_user_not_per_clone(isolated_base: Path) -> None:
    path = door_credential.secret_path()
    assert path.parent == isolated_base
    assert path.parent == door_credential.warm_dir()
    clone_dir = breadcrumb.svc_dir(isolated_base / "any-clone")
    assert clone_dir.parent == path.parent
    assert path.parent != clone_dir


def test_ensure_secret_mints_once_and_never_rotates(isolated_base: Path) -> None:
    first = door_credential.ensure_secret()
    assert len(first) == door_credential.SECRET_NBYTES * 2
    for _ in range(5):
        assert door_credential.ensure_secret() == first
    assert door_credential.read_secret() == first


def test_mint_leaves_no_tmp_behind_and_writes_atomically(isolated_base: Path) -> None:
    door_credential.ensure_secret()
    leftovers = list(isolated_base.glob("*.tmp"))
    assert leftovers == []
    assert door_credential.secret_path().is_file()


def test_read_secret_is_none_before_any_mint(isolated_base: Path) -> None:
    assert door_credential.read_secret() is None


def test_read_secret_treats_empty_file_as_absent(isolated_base: Path) -> None:
    door_credential.secret_path().write_text("", encoding="utf-8")
    assert door_credential.read_secret() is None


def test_verify_rejects_absent_on_either_side(isolated_base: Path) -> None:
    held = door_credential.ensure_secret()
    assert door_credential.verify(held, held) is True
    assert door_credential.verify(held, None) is False
    assert door_credential.verify(None, held) is False
    assert door_credential.verify("", held) is False
    assert door_credential.verify(held, "") is False
    flipped = held[:-1] + ("a" if held[-1] != "a" else "b")
    assert flipped != held
    assert door_credential.verify(flipped, held) is False


def test_credential_header_read_case_insensitively() -> None:
    assert (
        door_credential.credential_from_headers({"x-coordinator-door-key": "abc"})
        == "abc"
    )
    assert (
        door_credential.credential_from_headers({"X-COORDINATOR-DOOR-KEY": "abc"})
        == "abc"
    )


def test_empty_header_is_absent_not_a_blank_credential() -> None:
    assert door_credential.credential_from_headers({}) is None
    assert (
        door_credential.credential_from_headers({door_credential.CREDENTIAL_HEADER: ""})
        is None
    )


def test_boundary_assertion_returns_evidence(isolated_base: Path) -> None:
    evidence = door_credential.assert_directory_excludes_others()
    assert isinstance(evidence, str) and evidence


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are inert on Windows")
def test_boundary_assertion_rejects_a_world_readable_dir(isolated_base: Path) -> None:
    os.chmod(isolated_base, 0o755)
    with pytest.raises(door_credential.DirectoryBoundaryError):
        door_credential.assert_directory_excludes_others()


@pytest.mark.skipif(sys.platform != "win32", reason="DACL is the Windows boundary")
def test_windows_boundary_names_only_system_admins_and_owner(isolated_base: Path) -> None:
    sddl = door_credential.assert_directory_excludes_others()
    lowered = sddl.lower()
    assert ";;;wd)" not in lowered
    assert ";;;au)" not in lowered
    assert ";;;bu)" not in lowered
