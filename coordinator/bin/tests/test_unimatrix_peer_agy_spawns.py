"""Behavior pins for the agy peer adapter and the agy-run leg."""
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if sys.platform == "win32":
    pytest.skip("fake leg uses an AF_UNIX socket", allow_module_level=True)

agy = load("unimatrix-peer-agy")
leg = load("unimatrix-agy-leg")


def start_leg(monkeypatch, tmp_path, token):
    """Run leg.main in a daemon thread on a relative socket; returns the spool path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leg, "LOG", str(tmp_path / "conv-1.log"))
    monkeypatch.setenv("ANTIGRAVITY_CONVERSATION_ID", "conv-1")
    monkeypatch.delenv("AGY_LEG_SPOOL", raising=False)
    monkeypatch.delenv("AGY_LS_HTTP", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    if token:
        monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", token)
    else:
        monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    start_leg.thread = threading.Thread(target=leg.main, args=("pend.sock",), daemon=True)
    start_leg.thread.start()
    for _ in range(200):
        if (tmp_path / "pend.sock").exists():
            break
        time.sleep(0.01)
    return tmp_path / "conv-1.queue.jsonl"


def run_leg_with_parent(monkeypatch, tmp_path, parent_pid):
    """Run leg.main pending with AGY_LEG_PARENT_PID set; returns (thread, socket path)."""
    monkeypatch.setattr(leg, "PARENT_POLL_SECS", 0.05)
    monkeypatch.setenv("AGY_LEG_PARENT_PID", str(parent_pid))
    start_leg(monkeypatch, tmp_path, token=None)
    return start_leg.thread,tmp_path / "pend.sock"


def test_leg_exits_and_unlinks_its_endpoint_when_its_agy_parent_is_gone(monkeypatch, tmp_path):
    import subprocess
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    t, sock = run_leg_with_parent(monkeypatch, tmp_path, dead.pid)
    t.join(timeout=5)
    assert not t.is_alive()
    assert not sock.exists()
