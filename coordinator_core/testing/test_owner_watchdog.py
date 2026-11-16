"""Tests for the owner-death watchdog plugin: the session and its children die with the owner."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil
import pytest

from coordinator_core.testing import owner_watchdog as ow
from coordinator_core.testing import watch_parent
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete

_REPO = Path(__file__).resolve().parents[2]


def _wait_for(event: threading.Event, seconds: float) -> bool:
    return event.wait(seconds)


@pytest.mark.deliberate_wall_clock(reason="bounds that a live owner raises no false alarm, a wait CPU time cannot see")
def test_live_owner_does_not_fire():
    fired = threading.Event()
    chain = [(os.getpid(), watch_parent.process_create_time(os.getpid()))]
    ow.start(chain, interval=0.02, exit_fn=lambda code: fired.set())
    assert not _wait_for(fired, 0.3)


def test_dead_or_reused_owner_fires_with_137_after_killing_descendants(monkeypatch):
    killed = []
    monkeypatch.setattr(ow, "kill_descendants", lambda root_pid=None: killed.append(True))
    codes = []
    fired = threading.Event()

    def leave(code):
        codes.append(code)
        fired.set()

    live_pid = os.getpid()
    reused = [(live_pid, watch_parent.process_create_time(live_pid) + 1000.0)]
    ow.start(reused, interval=0.02, exit_fn=leave)
    assert _wait_for(fired, 5)
    assert codes == [137]
    assert killed == [True]


@pytest.mark.spawns_process
def test_kill_descendants_kills_the_whole_subtree_and_spares_the_caller():
    token = "ownerwdtok" + uuid.uuid4().hex[:12]
    sleeper = "import time,sys\ntime.sleep(120)"
    parent_src = (
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',%r,%r])\n"
        "time.sleep(120)" % (sleeper, token)
    )
    child = subprocess.Popen([sys.executable, "-c", parent_src, token], **no_console_creationflags())
    try:
        deadline = time.monotonic() + 20
        while len(psutil.Process().children(recursive=True)) < 2:
            assert time.monotonic() < deadline, "subtree never started"
            time.sleep(0.05)
        tree = psutil.Process().children(recursive=True)
        ow.kill_descendants()
        gone, alive = psutil.wait_procs(tree, timeout=10)
        assert alive == []
    finally:
        with contextlib.suppress(psutil.Error):
            child.kill()
    assert psutil.pid_exists(os.getpid())


def test_conftest_registers_the_plugin():
    src = (_REPO / "conftest.py").read_text(encoding="utf-8")
    assert '"coordinator_core.testing.owner_watchdog"' in src


def _timeout_named_python(directory: Path) -> Path:
    """A copy (symlink off Windows) of this interpreter named `timeout`, the shell-name the
    ancestor chain walks through, so the test needs no real coreutils `timeout`."""
    name = "timeout.exe" if os.name == "nt" else "timeout"
    target = directory / name
    if os.name == "nt":
        shutil.copy2(sys.executable, target)
    else:
        target.symlink_to(sys.executable)
    return target


_WRAPPER = """
import subprocess, sys
sys.exit(subprocess.Popen(sys.argv[1:]).wait())
"""

_OWNER = """
import subprocess, sys, time
wrapper = sys.argv[1:]
subprocess.Popen(wrapper)
time.sleep(300)
"""

_SLEEP_TEST = """
import subprocess, sys, time

def test_sleep():
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", %r])
    time.sleep(120)
"""


def _tagged(token):
    found = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(token in a for a in (p.info["cmdline"] or [])):
                found.append(p)
        except psutil.Error:
            pass
    return found


def _is_pytest_or_its_child(p):
    """The pytest session itself (`python -m pytest`, not the wrapper that merely carries those
    args) or the sleeper it spawned. Both exist only once the session has configured, so the
    owner is killed after the watchdog armed its chain."""
    cl = p.info["cmdline"] or []
    return cl[1:3] == ["-m", "pytest"] or "time.sleep(120)" in " ".join(cl)


@pytest.mark.spawns_process
@pytest.mark.deliberate_wall_clock(reason="bounds that the session and its child die within seconds of the owner instead of their 120s sleep, a wait CPU time cannot see")
def test_pytest_and_its_child_die_within_seconds_of_the_owner(tmp_path):
    token = "ownerwdtok" + uuid.uuid4().hex[:12]
    (tmp_path / ("test_sleep_%s.py" % token)).write_text(_SLEEP_TEST % token)
    (tmp_path / "wrapper.py").write_text(_WRAPPER)
    (tmp_path / "owner.py").write_text(_OWNER)
    timeout_py = _timeout_named_python(tmp_path)
    pytest_argv = ["-m", "pytest", "-p", "no:cacheprovider", "-p", "coordinator_core.testing.owner_watchdog",
                   "-q", "test_sleep_%s.py" % token]
    # The copied `timeout` has no site-packages: the wrapper must launch the real interpreter.
    cmd = [sys.executable, "owner.py", str(timeout_py), "wrapper.py", sys.executable, *pytest_argv]
    env = dict(os.environ, PYTHONPATH=str(_REPO), COORDINATOR_PYTEST_DEPTH="0")
    kw = {**no_console_creationflags()}
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    owner = subprocess.Popen(cmd, cwd=tmp_path, env=env, **kw)
    try:
        deadline = time.monotonic() + 40
        while sum(1 for p in _tagged(token) if _is_pytest_or_its_child(p)) < 2:
            assert owner.poll() is None, "owner exited early"
            assert time.monotonic() < deadline, "pytest and its child never both started"
            time.sleep(0.2)
        owner.kill()
        deadline = time.monotonic() + 15
        while _tagged(token) and time.monotonic() < deadline:
            time.sleep(0.2)
        survivors = [p.pid for p in _tagged(token)]
    finally:
        for p in _tagged(token):
            # Orphans of the killed owner are outside this test's tree: the conftest kill guard
            # refuses them, so a failed run's leftovers are best-effort.
            with contextlib.suppress(Exception):
                p.kill()
        owner.kill()
    assert survivors == []
