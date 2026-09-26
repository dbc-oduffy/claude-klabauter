
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.whoami_run_tests import main
from coordinator_core.win_portability import no_console_passthrough_kwargs

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


class _FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_provisions_venv_when_sentinel_absent(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    suppression_kwargs: list[dict] = []

    def _fake_run(cmd, check=False, cwd=None, env=None, **kwargs):
        calls.append(list(cmd))
        suppression_kwargs.append(kwargs)
        if cmd[:3] == ["python3", "-m", "venv"]:
            Path(cmd[3]).mkdir(parents=True, exist_ok=True)
        return _FakeCompleted(0)

    with patch("coordinator_core.ops.whoami_run_tests.subprocess.run", side_effect=_fake_run):
        rc = main([], base_dir=str(tmp_path))

    assert rc == 0
    sentinel = tmp_path / ".venv" / ".deps-installed"
    assert sentinel.is_file()
    assert len(calls) == 5
    assert calls[0][:3] == ["python3", "-m", "venv"]
    assert calls[-1][1:3] == ["-m", "pytest"]
    expected = no_console_passthrough_kwargs()
    assert all(kwargs == expected for kwargs in suppression_kwargs)


def test_skips_provisioning_when_sentinel_present(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / ".deps-installed").touch()

    calls: list[list[str]] = []
    suppression_kwargs: list[dict] = []

    def _fake_run(cmd, check=False, cwd=None, env=None, **kwargs):
        calls.append(list(cmd))
        suppression_kwargs.append(kwargs)
        return _FakeCompleted(0)

    with patch("coordinator_core.ops.whoami_run_tests.subprocess.run", side_effect=_fake_run):
        rc = main(["tests/test_machine.py"], base_dir=str(tmp_path))

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][1:3] == ["-m", "pytest"]
    assert calls[0][-1] == "tests/test_machine.py"
    assert suppression_kwargs[0] == no_console_passthrough_kwargs()


def test_pytest_nonzero_exit_passes_through(tmp_path: Path) -> None:
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / ".deps-installed").touch()

    def _fake_run(cmd, check=False, cwd=None, env=None, **kwargs):
        return _FakeCompleted(4)

    with patch("coordinator_core.ops.whoami_run_tests.subprocess.run", side_effect=_fake_run):
        rc = main(["tests/does_not_exist.py"], base_dir=str(tmp_path))

    assert rc == 4


def test_provisioning_failure_removes_half_built_venv_and_returns_1(tmp_path: Path) -> None:
    def _fake_run(cmd, check=False, cwd=None, env=None, **kwargs):
        if cmd[:3] == ["python3", "-m", "venv"]:
            (tmp_path / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
            return _FakeCompleted(0)
        raise subprocess.CalledProcessError(1, cmd)

    with patch("coordinator_core.ops.whoami_run_tests.subprocess.run", side_effect=_fake_run):
        rc = main([], base_dir=str(tmp_path))

    assert rc == 1
    assert not (tmp_path / ".venv").exists()


def test_default_base_dir_is_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / ".deps-installed").touch()

    def _fake_run(cmd, check=False, cwd=None, env=None, **kwargs):
        assert cwd == str(tmp_path)
        return _FakeCompleted(0)

    with patch("coordinator_core.ops.whoami_run_tests.subprocess.run", side_effect=_fake_run):
        rc = main([])

    assert rc == 0
