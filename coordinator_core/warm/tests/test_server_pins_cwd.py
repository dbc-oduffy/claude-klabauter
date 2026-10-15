"""The warm server must not keep its spawner's cwd: a deleted spawner temp dir
makes every later request raise in ``os.getcwd()`` and close unanswered."""

from __future__ import annotations

import inspect
import os

from coordinator_core.warm import server


def test_pin_cwd_moves_off_a_deleted_spawner_dir(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    engine_root.mkdir()
    spawner = tmp_path / "session-tmp"
    spawner.mkdir()
    monkeypatch.chdir(spawner)
    spawner.rmdir()

    server._pin_cwd(engine_root)

    assert os.getcwd() == str(engine_root)


def test_pin_cwd_failure_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    server._pin_cwd(tmp_path / "missing")

    assert os.getcwd() == str(tmp_path)
    assert "could not pin cwd" in capsys.readouterr().err


def test_boot_pins_cwd_after_winning_the_election():
    boot = inspect.getsource(server._run_guarded)
    assert "_pin_cwd(repo_root)" in boot
    assert boot.index("ElectionLost") < boot.index("_pin_cwd(repo_root)") < boot.index("_preload_op_registry()")
