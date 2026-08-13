"""
Tests for coordinator_core.ops.whoami_run_tests.

Port of: run-tests.sh (coordinator-claude 6fb5fb37, 2026-07-22). Golden-oracle parity
targets (captured 2026-07-17): fresh provision -> full suite green (exit 0); provisioned +
selector -> exit 0; provisioned + missing-file selector -> pytest exit 4.
Full end-to-end venv provisioning is exercised only in the `slow` marker
(real network/pip I/O); the fast tests fake out subprocess/pytest to pin the
control-flow contract (sentinel gating, ERR-path venv cleanup, argv passthrough).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.whoami_run_tests import main
from coordinator_core.win_portability import no_console_creationflags


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
    # venv create + upgrade pip + editable install + pytest install + final pytest run
    assert len(calls) == 5
    assert calls[0][:3] == ["python3", "-m", "venv"]
    assert calls[-1][1:3] == ["-m", "pytest"]
    # Every spawn is suppressed -- pins whoami_run_tests to console-popup suppression.
    expected = no_console_creationflags()
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
    assert suppression_kwargs[0] == no_console_creationflags()


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
            # Simulate venv creation succeeding on disk (as real `python3 -m venv` would)
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
