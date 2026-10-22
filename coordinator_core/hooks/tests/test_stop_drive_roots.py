"""Stop-leg drive-root scan covers every fixed local drive, not only cwd/repo/home anchors."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import stop_dispatch as sd


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DRIVE_ROOTS", raising=False)
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOT_BASELINE", str(tmp_path / "baseline.json"))


def test_drive_roots_include_every_fixed_drive(monkeypatch, tmp_path):
    other = tmp_path / "other-drive"
    monkeypatch.setattr(sd, "_fixed_drive_roots", lambda: [str(other)])
    assert str(other) in sd._drive_roots(str(tmp_path), None)


def test_override_env_still_wins(monkeypatch, tmp_path):
    only = tmp_path / "only"
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(only))
    monkeypatch.setattr(sd, "_fixed_drive_roots", lambda: ["Z:\\"])
    assert sd._drive_roots(str(tmp_path), None) == [str(only)]


def test_new_dir_on_a_non_cwd_drive_fires(monkeypatch, tmp_path):
    other = tmp_path / "other-drive"
    other.mkdir()
    (other / "old").mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(sd, "_fixed_drive_roots", lambda: [str(other)])
    monkeypatch.setattr(sd, "show_toplevel", lambda _c: None)
    params = {"payload": {"cwd": str(cwd), "session_id": "s"}}
    sd._guard_drive_root_new_dir_handler(params)  # seeds the baseline silently
    (other / "e").mkdir()
    res = sd._guard_drive_root_new_dir_handler(params)
    assert "e" in str(res) and "guard-drive-root-new-dir" in str(res)
