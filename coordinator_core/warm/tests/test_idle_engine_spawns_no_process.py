"""The idle warm engine starts no process: nothing asked of it, nothing spawned.

Boots an isolated engine with no dispatch (no pool fill), attaches a job-object accountant before
the window opens, holds it idle for IDLE_WINDOW_SECS, and asserts the engine job's TotalProcesses
delta is zero. On failure the message names every process started in the window: image, and where
still alive when sampled, command line and parent pid.

`push_cadence._sweep_due` seeds its clock on the first idle tick, so the first sweep lands one
full interval after boot, outside the window. The boot-time http-listener spawn is let to settle
before the window opens.

Axis 2 (`ServerVersionState.refresh`) is request-driven with a stat stamp and no git leg, so the
idle engine starts nothing. Run explicitly with `-m cadence`.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.engine_op_overhead import WarmEngine, isolated_warm_engine
from coordinator_core.benchmarks.process_time import IS_WINDOWS, LiveTreeAccountant, _JobImageNamer

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Question: "does the engine start any process with nothing asked of it?" Spans two server
#: idle-watchdog polls (_IDLE_WATCHDOG_POLL_SECS 5.0) and six request-path refresh intervals
#: (_REFRESH_INTERVAL_SECS 2.0); far below push_cadence.PUSH_CADENCE_INTERVAL_SECS (600).
IDLE_WINDOW_SECS = 12.0

#: Question: "how often is the live process list sampled for command lines?" Short enough to
#: catch a git child that lives a few tens of milliseconds.
_SAMPLE_INTERVAL_SECS = 0.01

#: Question: "how long must the descendant set be unchanged before boot is over?" / "how long
#: do we wait for that at most?"
_BOOT_QUIET_SECS = 3.0
_BOOT_SETTLE_MAX_SECS = 45.0

_SETTINGS_HOME = Path(
    os.environ.get("COORDINATOR_SETTINGS_HOME")
    or Path(os.path.expanduser("~")) / ".coordinator-claude-settings"
)
_PING_DOOR = _SETTINGS_HOME / "bin" / "coordinator-invoke.exe"


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def engine() -> WarmEngine:
    if not IS_WINDOWS:
        pytest.skip("idle-spawn gate is Windows-only: job-object accounting exists only there")
    if not _PING_DOOR.exists():
        pytest.skip(f"{_PING_DOOR} not installed on this box")
    with isolated_warm_engine(_source_root(), entrypoints=["coordinator-invoke"]) as warm:
        yield warm


def _sample_descendants(pid: int, seen: dict, stop: threading.Event) -> None:
    import psutil

    while not stop.is_set():
        try:
            for child in psutil.Process(pid).children(recursive=True):
                if child.pid in seen:
                    continue
                try:
                    seen[child.pid] = {
                        "image": child.name().lower(),
                        "cmdline": " ".join(child.cmdline())[:240],
                        "ppid": child.ppid(),
                    }
                except psutil.Error:
                    pass
        except psutil.Error:
            pass
        stop.wait(_SAMPLE_INTERVAL_SECS)


def _wait_boot_spawns_settled(pid: int) -> None:
    """Boot starts the http listener on a thread after the engine answers; that spawn is boot
    cost, not idle cost, so the window opens only after the descendant set has been quiet."""
    import psutil

    deadline = time.monotonic() + _BOOT_SETTLE_MAX_SECS
    quiet_since = time.monotonic()
    known: set = set()
    listener_seen = False
    while time.monotonic() < deadline:
        try:
            children = psutil.Process(pid).children(recursive=True)
            now = {c.pid for c in children}
            if not listener_seen:
                for c in children:
                    try:
                        listener_seen = listener_seen or "supervisor.py" in " ".join(c.cmdline())
                    except psutil.Error:
                        pass
        except psutil.Error:
            return
        if now - known:
            known |= now
            quiet_since = time.monotonic()
        elif listener_seen and time.monotonic() - quiet_since >= _BOOT_QUIET_SECS:
            return
        time.sleep(_SAMPLE_INTERVAL_SECS)


def test_idle_engine_starts_no_process(engine):
    _wait_boot_spawns_settled(engine.pid)
    with LiveTreeAccountant(engine.pid) as acct:
        seen: dict = {}
        stop = threading.Event()
        sampler = threading.Thread(target=_sample_descendants, args=(engine.pid, seen, stop), daemon=True)
        namer = _JobImageNamer(acct._job)
        before = acct.snapshot()
        namer.start()
        sampler.start()
        try:
            time.sleep(IDLE_WINDOW_SECS)
        finally:
            after = acct.snapshot()
            stop.set()
            sampler.join()
            images = namer.stop()

    procs = after["procs"] - before["procs"]
    ms = after["process_time_ms"] - before["process_time_ms"]
    print(
        f"ENGINE-IDLE: ms_per_s={ms / IDLE_WINDOW_SECS:.3f} "
        f"procs_per_s={procs / IDLE_WINDOW_SECS:.3f} procs={procs} window_s={IDLE_WINDOW_SECS}"
    )
    named = [
        f"pid {pid}: {info['image']} ppid={info['ppid']} cmd={info['cmdline']!r}"
        for pid, info in sorted(seen.items())
    ]
    named += [
        f"pid {pid}: {image} (cmdline not sampled)" for pid, image in sorted(images.items()) if pid not in seen
    ]
    assert procs == 0, (
        f"idle engine started {procs} process(es) in {IDLE_WINDOW_SECS}s "
        f"({len(named)} named, {procs - len(named)} unnamed):\n" + "\n".join(named)
    )
