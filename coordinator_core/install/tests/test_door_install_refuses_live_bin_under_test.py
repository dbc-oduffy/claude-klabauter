"""install_door / install_named_forwarder refuse the live settings bin while a test or sandbox
marker is set, and the root conftest points COORDINATOR_SETTINGS_HOME at a throwaway dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.install import door_install


def _stamped_engine(root: Path) -> Path:
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")
    return root


@pytest.fixture
def live_home(tmp_path, monkeypatch):
    """A stand-in for the operator's live settings home, named through the same seam the
    real one is (the forbid env), so the test never touches the real directory."""
    home = tmp_path / "account" / ".coordinator-claude-settings"
    home.mkdir(parents=True)
    monkeypatch.setenv(door_install._FORBID_REAL_SETTINGS_HOME_ENV, str(home))
    return home


def test_install_door_refuses_live_bin_before_any_write(tmp_path, live_home):
    engine = _stamped_engine(tmp_path / "engine")
    bin_dst = live_home / "bin"
    with pytest.raises(door_install.DoorInstallError, match="live settings home"):
        door_install.install_door(bin_dst, engine)
    assert not bin_dst.exists()


def test_install_door_refuses_a_nested_live_bin(tmp_path, live_home):
    engine = _stamped_engine(tmp_path / "engine")
    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(live_home / "x" / "bin", engine)
    assert not (live_home / "x").exists()


def test_sandbox_marker_alone_also_refuses(tmp_path, live_home, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    engine = _stamped_engine(tmp_path / "engine")
    with pytest.raises(door_install.DoorInstallError):
        door_install.install_door(live_home / "bin", engine)


def test_named_forwarder_refuses_live_bin_even_with_a_source(tmp_path, live_home):
    engine = _stamped_engine(tmp_path / "engine")
    src = tmp_path / "somewhere" / "door"
    src.parent.mkdir()
    src.write_bytes(b"x")
    with pytest.raises(door_install.DoorInstallError):
        door_install.install_named_forwarder(live_home / "bin", engine, "some-name", source=src)
    assert not (live_home / "bin").exists()


def test_scratch_bin_is_not_refused(tmp_path, live_home):
    # The other side of the decision: a bin outside the live home passes the guard.
    door_install._refuse_live_bin_under_test(tmp_path / "scratch" / "bin")


def test_refusal_is_inert_without_a_test_or_sandbox_marker(tmp_path, live_home, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    door_install._refuse_live_bin_under_test(live_home / "bin")


def test_settings_home_override_cannot_hide_the_live_home(tmp_path, live_home, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "decoy"))
    with pytest.raises(door_install.DoorInstallError):
        door_install._refuse_live_bin_under_test(live_home / "bin")


def test_root_conftest_isolates_settings_home():
    # Whichever conftest layer applies (root override, or coordinator_core's HOME quarantine),
    # a test's settings home is never the operator's live one.
    from coordinator_core import _settings_home

    live = door_install._real_settings_homes()
    assert os.path.normcase(os.path.realpath(_settings_home.settings_home())) not in live
