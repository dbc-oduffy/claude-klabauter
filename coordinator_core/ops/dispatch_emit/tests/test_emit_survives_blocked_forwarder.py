"""dispatch.emit (engine-ask route) returns its result envelope when the settings-home door forwarder cannot launch."""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_wiring import env  # noqa: F401

WINERROR_CODE_INTEGRITY = 4551
EXIT_PERMISSION_DENIED = 126


def _is_forwarder(argv) -> bool:
    first = argv if isinstance(argv, (str, os.PathLike)) else (argv[0] if argv else "")
    return Path(os.fspath(first)).name.startswith("coordinator-invoke")


class _Blocked(Exception):
    """Marker so a forwarder launch that escapes the emit is distinguishable in a failure."""


@pytest.fixture
def blocked_forwarder(tmp_path, monkeypatch):
    """A settings home holding an unlaunchable `coordinator-invoke`; every subprocess launch of it fails.

    Returns the list of attempted forwarder launches so a test can assert on them."""
    home = tmp_path / "settings-home"
    (home / "bin").mkdir(parents=True)
    for name in ("coordinator-invoke.exe", "coordinator-invoke.cmd", "coordinator-invoke"):
        (home / "bin" / name).write_bytes(b"not a runnable image")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_HOME", str(home))
    attempts: list = []
    mode = {"kind": "oserror"}

    def _fail(argv):
        attempts.append(argv)
        if mode["kind"] == "oserror":
            exc = OSError(13, "An Application Control policy has blocked this file")
            exc.winerror = WINERROR_CODE_INTEGRITY
            raise exc
        return EXIT_PERMISSION_DENIED

    real_run, real_popen = subprocess.run, subprocess.Popen

    def fake_run(argv, *a, **k):
        if _is_forwarder(argv):
            rc = _fail(argv)
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="bash: coordinator-invoke: Permission denied")
        return real_run(argv, *a, **k)

    class FakePopen(real_popen):
        def __init__(self, argv, *a, **k):
            if _is_forwarder(argv):
                _fail(argv)
                raise _Blocked(argv)
            super().__init__(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    def fake_system(cmd):
        if "coordinator-invoke" in str(cmd):
            attempts.append(cmd)
            return EXIT_PERMISSION_DENIED << 8
        return 0

    monkeypatch.setattr(os, "system", fake_system)
    return types.SimpleNamespace(attempts=attempts, mode=mode)


@pytest.mark.parametrize("kind", ["oserror", "exit126"])
def test_engine_ask_emit_returns_its_envelope_with_a_blocked_forwarder(env, blocked_forwarder, kind) -> None:  # noqa: F811
    blocked_forwarder.mode["kind"] = kind
    reply = op._dispatch_emit({"ask": "add a widget"}, repo_root=env.root)
    assert isinstance(reply, dict)
    assert "error" not in reply, reply
    assert reply.get("run_id") and reply.get("path"), reply
    assert Path(reply["path"]).is_file()


def test_emit_never_launches_the_forwarder_itself(env, blocked_forwarder) -> None:  # noqa: F811
    op._dispatch_emit({"ask": "add a widget"}, repo_root=env.root)
    assert blocked_forwarder.attempts == []


def test_the_injected_block_is_live(blocked_forwarder) -> None:
    """Pins the fixture: a launch of the forwarder does fail with the SAC signature, so the emit tests are not vacuous."""
    exe = Path(os.environ["COORDINATOR_SETTINGS_HOME"]) / "bin" / "coordinator-invoke.exe"
    with pytest.raises(OSError) as caught:
        subprocess.run([str(exe), "--version"])
    assert caught.value.winerror == WINERROR_CODE_INTEGRITY
    blocked_forwarder.mode["kind"] = "exit126"
    done = subprocess.run([str(exe), "--version"])
    assert done.returncode == EXIT_PERMISSION_DENIED and "Permission denied" in done.stderr
