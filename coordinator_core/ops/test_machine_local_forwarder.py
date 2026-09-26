"""Tests for coordinator_core.ops.machine_local_forwarder.

Port-parity coverage for coordinator/bin/machine-local (DOE-PORT bin-entrypoint
variant). Exercises delegation to coordinator_core.bare_forwarder.forward via
monkeypatched os.access/os.execv/sys.exit, matching the test shape used for other
bare_forwarder-backed shims.
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.ops import machine_local_forwarder as mlf


def test_forwards_to_real_machine_local_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    real = tmp_path / ".claude" / "bin" / "machine-local"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\necho hi\n")
    real.chmod(0o755)

    monkeypatch.setattr(
        "coordinator_core.bare_forwarder.is_executable",
        lambda path: str(path) == str(real),
    )

    captured = {}

    def fake_execv(path, args):
        captured["path"] = path
        captured["args"] = args
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr("sys.argv", ["machine-local", "SHOULD-NOT-BE-FORWARDED"])

    with pytest.raises(SystemExit) as exc_info:
        mlf.main(["get", "repos.foo"])

    assert exc_info.value.code == 0
    assert captured["path"] == str(real)
    assert captured["args"] == [str(real), "get", "repos.foo"]


def test_exits_127_when_real_binary_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr("coordinator_core.bare_forwarder.is_executable", lambda path: False)

    with pytest.raises(SystemExit) as exc_info:
        mlf.main([])

    assert exc_info.value.code == 127
    err = capsys.readouterr().err
    assert "machine-local: resolver not installed at" in err
    assert "run /coordinator:setup (Phase 3)" in err


def test_falls_back_to_home_when_claude_home_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # `home_dir()`'s fallback is `Path.home()`, which reads USERPROFILE on
    # `home_dir` docstring: "already honours USERPROFILE" -- HOME is a
    monkeypatch.setattr("coordinator_core.bare_forwarder.home_dir", lambda: tmp_path)
    real = tmp_path / ".claude" / "bin" / "machine-local"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)

    monkeypatch.setattr(
        "coordinator_core.bare_forwarder.is_executable",
        lambda path: str(path) == str(real),
    )

    def fake_execv(path, args):
        raise SystemExit(0)

    monkeypatch.setattr(os, "execv", fake_execv)

    with pytest.raises(SystemExit) as exc_info:
        mlf.main([])

    assert exc_info.value.code == 0
