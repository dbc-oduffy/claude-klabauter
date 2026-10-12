"""cli_shared.managed_temp_root is pinned equal to coordinator_core.temp_layout.managed_tempdir."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import cli_shared  # noqa: E402
from coordinator_core import temp_layout  # noqa: E402


@pytest.fixture
def fake_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def test_fleet_root_equals_temp_layout(fake_tmp):
    mirror = Path(cli_shared.managed_temp_root())
    assert mirror == temp_layout.managed_tempdir()
    assert mirror == fake_tmp / "coordinator" / temp_layout.FLEET_DIRNAME
    assert mirror.is_dir()


def test_repo_root_equals_temp_layout(fake_tmp):
    repo = fake_tmp / "some-repo"
    mirror = Path(cli_shared.managed_temp_root(repo))
    assert mirror == temp_layout.managed_tempdir(repo)
    assert mirror == fake_tmp / "coordinator" / "some-repo"
    assert mirror.is_dir()


def test_string_repo_root_with_trailing_separator(fake_tmp):
    assert Path(cli_shared.managed_temp_root(str(fake_tmp / "r") + "/")) == (
        temp_layout.managed_tempdir(fake_tmp / "r")
    )


def test_reserved_fleet_name_refused_like_temp_layout(fake_tmp):
    with pytest.raises(ValueError):
        temp_layout.managed_tempdir(fake_tmp / "_fleet")
    with pytest.raises(ValueError):
        cli_shared.managed_temp_root(fake_tmp / "_fleet")
