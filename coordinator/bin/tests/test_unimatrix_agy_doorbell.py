"""unimatrix-agy-doorbell: one claimed message per ring, rung marker, silent exit when agy is gone."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent
SCRIPT = BIN / "unimatrix-agy-doorbell.py"
CONV = "conv-abc_123"


def _load():
    spec = importlib.util.spec_from_file_location("unimatrix_agy_doorbell_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("AGY_LEG_SPOOL", raising=False)
    monkeypatch.delenv("AGY_LEG_PARENT_PID", raising=False)
    return tmp_path


def _ddir(home):
    d = home / "unimatrix" / "agy-legs" / f"{CONV}.doorbell"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drop(ddir, msg_id, text):
    tmp = ddir / f"{msg_id}.tmp"
    tmp.write_text(json.dumps({"id": msg_id, "text": text}), encoding="utf-8")
    os.replace(tmp, ddir / f"{msg_id}.msg")


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_ring_prints_one_message_and_leaves_rung(home):
    ddir = _ddir(home)
    _drop(ddir, "m1", "[from peer-x on host] hello there")
    _drop(ddir, "m2", "second message")
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(home)}
    r = subprocess.run([sys.executable, str(SCRIPT), "--conversation", CONV], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "hello there" in r.stdout
    assert "second message" not in r.stdout
    assert "Reply to peer-x" in r.stdout
    assert f"--conversation {CONV}" in r.stdout and "Re-arm" in r.stdout
    assert (ddir / "m1.rung").is_file() and not (ddir / "m1.msg").exists()
    assert (ddir / "m2.msg").is_file()  # a second message waits for the re-armed doorbell
    assert not list(ddir.glob("armed.*"))


def test_message_dropped_while_armed_wakes_it(home):
    ddir = _ddir(home)
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(home)}
    p = subprocess.Popen([sys.executable, str(SCRIPT), "--conversation", CONV], env=env,
                         stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not list(ddir.glob("armed.*")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert list(ddir.glob("armed.*")), "armed marker never appeared"
        _drop(ddir, "late", "wake up")
        out, _ = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0 and "wake up" in out
    assert (ddir / "late.rung").is_file()


def test_dead_session_exits_zero_silent_and_removes_armed(home, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "LIVENESS_SECS", 0.0)
    monkeypatch.setattr(mod, "POLL_SECS", 0.01)
    monkeypatch.setenv("AGY_LEG_PARENT_PID", str(_dead_pid()))
    assert mod.ring_once(CONV) == 0
    assert capsys.readouterr().out == ""
    assert not list(_ddir(home).glob("armed.*"))


def test_live_session_keeps_waiting(home, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "LIVENESS_SECS", 0.0)
    monkeypatch.setattr(mod, "POLL_SECS", 0.01)
    monkeypatch.setenv("AGY_LEG_PARENT_PID", str(os.getpid()))
    assert mod.session_gone(CONV, time.time()) is False


def test_claim_loses_to_a_peer_that_already_claimed(home, monkeypatch):
    mod = _load()
    ddir = _ddir(home)
    _drop(ddir, "m1", "x")
    real = os.replace

    def peer_wins(src, dst):
        real(src, dst)  # the PreInvocation backstop claimed it first
        raise FileNotFoundError(src)

    monkeypatch.setattr(mod.os, "replace", peer_wins)
    assert mod.claim_next(ddir) is None


def test_agy_leg_spool_override_moves_the_doorbell_dir(home, monkeypatch, tmp_path):
    mod = _load()
    spool = tmp_path / "elsewhere" / f"{CONV}.queue.jsonl"
    monkeypatch.setenv("AGY_LEG_SPOOL", str(spool))
    assert mod.doorbell_dir(CONV) == spool.parent / f"{CONV}.doorbell"


def test_second_doorbell_exits_at_once_while_one_is_armed(home):
    ddir = _ddir(home)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        (ddir / f"armed.{holder.pid}").write_text(str(holder.pid), encoding="utf-8")
        _drop(ddir, "m1", "must stay unclaimed")
        env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(home)}
        r = subprocess.run([sys.executable, str(SCRIPT), "--conversation", CONV], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout == ""
        assert (ddir / "m1.msg").is_file() and not (ddir / "m1.rung").exists()
        assert (ddir / f"armed.{holder.pid}").is_file()  # the live holder's marker is untouched
    finally:
        holder.kill()
        holder.wait()


def test_stale_armed_marker_of_a_dead_pid_does_not_block_and_is_swept(home):
    mod = _load()
    ddir = _ddir(home)
    stale = ddir / f"armed.{_dead_pid()}"
    stale.write_text("x", encoding="utf-8")
    assert mod.live_peer_armed(ddir) is False
    assert not stale.exists()


def test_help_and_bad_conversation(home):
    r = subprocess.run([sys.executable, str(SCRIPT), "-h"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "--conversation" in r.stdout
    r = subprocess.run([sys.executable, str(SCRIPT), "--conversation", "../evil"], capture_output=True,
                       text=True, timeout=60, env={k: v for k, v in os.environ.items()
                                                    if k != "ANTIGRAVITY_CONVERSATION_ID"})
    assert r.returncode != 0


def test_armed_marker_records_pid_and_proc_start(home):
    mod = _load()
    body = json.loads(mod._marker_body())
    assert body["pid"] == os.getpid() and body["procStart"]


def test_claimed_rung_is_marked_printed_after_ring(home):
    ddir = _ddir(home)
    _drop(ddir, "m1", "hello")
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(home)}
    subprocess.run([sys.executable, str(SCRIPT), "--conversation", CONV], env=env,
                   capture_output=True, text=True, timeout=60)
    assert json.loads((ddir / "m1.rung").read_text(encoding="utf-8")) == {"printed": True}


def test_of_two_simultaneous_arms_only_the_earlier_marker_keeps_the_doorbell(home):
    mod = _load()
    ddir = _ddir(home)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        mine = ddir / f"armed.{os.getpid()}"
        mine.write_text("x")
        theirs = ddir / f"armed.{holder.pid}"
        theirs.write_text("x")
        os.utime(mine, ns=(2_000_000_000, 2_000_000_000))
        os.utime(theirs, ns=(1_000_000_000, 1_000_000_000))
        assert mod.live_peer_armed(ddir, mod._marker_key(ddir, mine.name)) is True
        os.utime(theirs, ns=(3_000_000_000, 3_000_000_000))
        assert mod.live_peer_armed(ddir, mod._marker_key(ddir, mine.name)) is False
    finally:
        holder.kill()
        holder.wait()
