"""A real unimatrix-agy-leg.py subprocess exits with its agy session: no parent pid, lock gone, pid dead."""
import importlib.util
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
CONV = "0cf351a3-6538-4a9b-814c-52b1b75a596c"
LEG = BIN / "unimatrix-agy-leg.py"
WIN = sys.platform == "win32"
EXIT_WITHIN = 20


def _transport():
    spec = importlib.util.spec_from_file_location("lifecycle_transport", BIN / "unimatrix-local-transport.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Leg:
    """One leg subprocess over an endpoint under tmp_path; brain and presence dirs live there too."""

    def __init__(self, tmp_path, grace, parent_pid=None):
        self.tmp = tmp_path
        self.presence = tmp_path / "presence"
        self.presence.mkdir()
        (tmp_path / "brain").mkdir()
        self.endpoint = rf"\\.\pipe\LOCAL\agy-leg-life-{uuid.uuid4().hex}" if WIN else "leg.sock"
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGY_LEG_PARENT_PID", "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_ADDRESS")}
        env.update({
            "ANTIGRAVITY_CONVERSATION_ID": CONV,
            "AGY_BRAIN_DIR": str(tmp_path / "brain"),
            "AGY_LEG_LOG": str(tmp_path / "leg.log"),
            "AGY_LEG_SPOOL": str(tmp_path / "q.jsonl"),
            "COORDINATOR_SETTINGS_HOME": str(tmp_path / "settings"),
            "AGY_LEG_POLL_SECS": "0.1",
            "AGY_LEG_GRACE_SECS": str(grace),
        })
        if parent_pid is not None:
            env["AGY_LEG_PARENT_PID"] = str(parent_pid)
        self.proc = subprocess.Popen(
            [sys.executable, str(LEG), self.endpoint], cwd=tmp_path, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @property
    def log(self):
        path = self.tmp / "leg.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def wait_started(self):
        deadline = time.monotonic() + EXIT_WITHIN
        while "start" not in self.log and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "start" in self.log

    def hold_presence(self):
        path = self.presence / f"{CONV}.lock"
        path.write_bytes(b"\0")
        held = open(path, "r+b")
        assert _transport().lock_file(held) is True
        return held

    def endpoint_gone(self):
        return WIN or not (self.tmp / self.endpoint).exists()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


@pytest.fixture
def make_leg(tmp_path):
    legs = []

    def make(grace, parent_pid=None):
        leg = Leg(tmp_path, grace, parent_pid)
        legs.append(leg)
        return leg

    yield make
    for leg in legs:
        leg.close()


def test_leg_with_no_parent_pid_exits_once_its_session_is_gone_and_unlinks_its_endpoint(make_leg):
    leg = make_leg(grace=0.5)
    assert leg.proc.wait(timeout=EXIT_WITHIN) == 0
    assert "agy parent gone" in leg.log
    assert leg.endpoint_gone()


def test_leg_stays_up_while_the_presence_lock_is_held_and_exits_once_released(make_leg):
    leg = make_leg(grace=0.5)
    held = leg.hold_presence()
    try:
        leg.wait_started()
        time.sleep(2.0)  # four grace periods
        assert leg.proc.poll() is None
        assert leg.endpoint_gone() is WIN
    finally:
        held.close()
    assert leg.proc.wait(timeout=EXIT_WITHIN) == 0
    assert leg.endpoint_gone()


def test_leg_exits_when_its_recorded_agy_pid_is_dead_even_inside_the_grace(make_leg):
    dead = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    dead.wait()
    leg = make_leg(grace=3600, parent_pid=dead.pid)
    assert leg.proc.wait(timeout=EXIT_WITHIN) == 0
    assert leg.endpoint_gone()


def test_leg_with_a_live_recorded_agy_pid_keeps_serving_past_the_grace(make_leg):
    leg = make_leg(grace=0.2, parent_pid=os.getpid())
    leg.wait_started()
    time.sleep(1.5)
    assert leg.proc.poll() is None
