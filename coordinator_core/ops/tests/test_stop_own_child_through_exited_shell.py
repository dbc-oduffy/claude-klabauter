"""proc.stop_own_child stops a process its caller's session spawned through an intermediate shell that
has since exited, entering through the registered handler at default params on a real process."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import uuid

import psutil
import pytest

from coordinator_core.ops import worker_cleanup as wc
from coordinator_core.session.core import SESSION_ENV_PRECEDENCE

_BASE_REFUSAL = "pid is not a descendant of the worker root"

_GRANDCHILD = """\
import os, sys, time
pid_file = sys.argv[1]
with open(pid_file + ".tmp", "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
os.replace(pid_file + ".tmp", pid_file)
time.sleep(600)
"""


def _clean_env(session_id: str | None) -> dict:
    """A copy of this process's environment without any session slot, then `session_id` in the
    explicit slot; the test process's own os.environ is never touched."""
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV_PRECEDENCE}
    if session_id is not None:
        env["COORDINATOR_SESSION_ID"] = session_id
    return env


def _shell_hop(script: str, pid_file: str) -> list:
    if sys.platform == "win32":
        return ["cmd", "/c", "start", "", "/b", sys.executable, script, pid_file]
    return ["sh", "-c", f"{shlex.quote(sys.executable)} {shlex.quote(script)} {shlex.quote(pid_file)} &"]


def _tripwire_modules() -> list:
    """Every loaded module carrying the conftest foreign-kill tripwire (it can be imported under more
    than one name; each copy's `_refuse_foreign` reads its own globals)."""
    return [
        m
        for m in list(sys.modules.values())
        if hasattr(m, "ForeignProcessKill") and hasattr(m, "_is_own_process_tree") and hasattr(m, "_refuse_foreign")
    ]


@pytest.fixture
def spawn_behind_exited_shell(tmp_path, monkeypatch):
    """Factory: spawn `shell -> grandchild` from this process, wait for the shell to exit, return the
    grandchild's psutil.Process. Survivors are stopped by literal pid and create_time at teardown."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    # Root = the test process itself, so the descent refusal is the op's own text on any host.
    monkeypatch.setattr(wc, "_ROOT_NAMES", frozenset({os.path.splitext(psutil.Process().name().lower())[0]}))
    spawned: list = []
    # The grandchild is reparented off this tree, so the conftest foreign-kill tripwire would refuse it;
    # open it for the exact pids this fixture spawned, nothing else.
    for conftest in _tripwire_modules():
        monkeypatch.setattr(
            conftest,
            "_is_own_process_tree",
            lambda pid, _real=conftest._is_own_process_tree: _real(pid) or any(pid == p.pid for p in spawned),
        )

    def spawn(session_id: str | None) -> psutil.Process:
        tag = uuid.uuid4().hex
        script = tmp_path / f"grandchild_{tag}.py"
        script.write_text(_GRANDCHILD, encoding="utf-8")
        pid_file = tmp_path / f"grandchild_{tag}.pid"
        shell = subprocess.Popen(
            _shell_hop(str(script), str(pid_file)),
            env=_clean_env(session_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists(), "grandchild never reported its pid"
        gc = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
        spawned.append(gc)
        shell.wait(20)
        assert shell.poll() is not None, "the intermediate shell is still running"
        assert gc.is_running()
        assert gc.ppid() != os.getpid(), "grandchild is still this process's direct child"
        return gc

    yield spawn
    for gc in spawned:
        try:
            if gc.is_running():
                gc.kill()
                gc.wait(5)
        except psutil.Error:
            pass


def _stop(pid: int, session_id: str) -> dict:
    return wc._stop_handler(
        {"pid": pid, "caller_pid": os.getpid(), "caller_session_id": session_id}, repo_root=None
    )


def _gone(gc: psutil.Process) -> bool:
    try:
        return not gc.is_running() or gc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_grandchild_behind_exited_shell_is_stopped_by_the_session_that_spawned_it(spawn_behind_exited_shell):
    session = str(uuid.uuid4())
    gc = spawn_behind_exited_shell(session)

    assert _stop(gc.pid, session) == {"stopped": gc.pid}
    gc.wait(10)
    assert _gone(gc)


def test_tripwire_allowance_still_refuses_a_pid_the_fixture_did_not_spawn(spawn_behind_exited_shell):
    spawn_behind_exited_shell(str(uuid.uuid4()))
    for conftest in _tripwire_modules():
        _assert_still_refuses(conftest)


def _assert_still_refuses(conftest) -> None:
    assert conftest._is_own_process_tree(os.getppid()) is False
    with pytest.raises(conftest.ForeignProcessKill):
        conftest._refuse_foreign(os.getppid(), "psutil.Process.terminate")


def test_grandchild_behind_exited_shell_is_refused_to_another_session(spawn_behind_exited_shell):
    gc = spawn_behind_exited_shell(str(uuid.uuid4()))

    out = _stop(gc.pid, str(uuid.uuid4()))

    assert out.get("refused", "").startswith(_BASE_REFUSAL), out
    assert gc.is_running()


def test_grandchild_with_no_session_marker_is_refused_with_the_existing_reason(spawn_behind_exited_shell):
    gc = spawn_behind_exited_shell(None)

    out = _stop(gc.pid, str(uuid.uuid4()))

    assert out.get("refused", "").startswith(_BASE_REFUSAL), out
    assert gc.is_running()

