"""Tests for coordinator/bin/box-memory-wait-pid.py with injected waiters; no real spawn."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "box-memory-wait-pid.py"
_spec = importlib.util.spec_from_file_location("box_memory_wait_pid", TOOL)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def _inline(monkeypatch, tmp_path):
    """pytest runs under a Claude Code session: keep main() inline unless a test clears the marker."""
    monkeypatch.setenv("COORDINATOR_GUARD_DETACHED", "1")
    monkeypatch.delenv("COORDINATOR_GUARD_FAKE_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)


class _FakePopen:
    spawned: list = []

    def __init__(self, cmd, **kw):
        type(self).spawned.append((cmd, kw))
        self.pid = 4242


@pytest.fixture
def session(monkeypatch):
    import subprocess

    _FakePopen.spawned = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.delenv("COORDINATOR_GUARD_DETACHED", raising=False)
    monkeypatch.setenv("COORDINATOR_GUARD_FAKE_SESSION", "1")
    return _FakePopen


def test_relaunch_carries_result_and_marker_and_session_half_prints_file(session, tmp_path, capsys):
    res = tmp_path / "r" / "w.result"
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        mod.write_result(res, "EXITED pid=42 code=0 elapsed=1.0s")

    rc = mod.main(["42", "--result", str(res), "--timeout", "30"], sleep=sleep)
    cmd, kw = session.spawned[0]
    assert "--result" in cmd and str(res) in cmd and "--timeout" in cmd
    assert kw["env"]["COORDINATOR_GUARD_DETACHED"] == "1"
    cap = capsys.readouterr()
    assert rc == 0 and cap.out == "EXITED pid=42 code=0 elapsed=1.0s\n"
    assert "DETACHED box-memory-wait-pid" in cap.err and "DETACHED" not in cap.out


def test_relaunch_appends_default_result(session, capsys):
    mod.main(["42", "--timeout", "0.01"], sleep=lambda s: None, clock=iter([0.0, 1.0, 2.0, 3.0]).__next__)
    cmd, _ = session.spawned[0]
    assert cmd[cmd.index("--result") + 1].endswith("wait-pid-42.result")
    assert capsys.readouterr().out == "TIMEOUT pid=42\n"


def test_existing_result_short_circuits(session, tmp_path, capsys):
    res = tmp_path / "w.result"
    mod.write_result(res, "EXITED pid=42 code=3 elapsed=2.0s")
    assert mod.main(["42", "--result", str(res)], waiter=lambda *a: pytest.fail("waited")) == 0
    assert capsys.readouterr().out == "EXITED pid=42 code=3 elapsed=2.0s\n"
    assert session.spawned == []


@pytest.mark.parametrize("stale", ["NO-SUCH-PID pid=9", "TIMEOUT pid=9"])
def test_existing_nonexit_result_is_stale_and_wait_reruns(tmp_path, capsys, stale):
    res = tmp_path / "w.result"
    mod.write_result(res, stale)
    assert mod.main(["9", "--result", str(res)], waiter=lambda p, i, t: ("exited", 0)) == 0
    assert capsys.readouterr().out.startswith("EXITED pid=9")
    assert res.read_text().startswith("EXITED pid=9")


def test_detached_half_writes_result_atomically(tmp_path, capsys, monkeypatch):
    res = tmp_path / "w.result"
    seen = []
    real_replace = mod.os.replace

    def replace(src, dst):
        seen.append((Path(src).name.endswith(".tmp"), Path(dst) == res, res.exists()))
        real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", replace)
    assert mod.main(["42", "--result", str(res)], waiter=lambda p, i, t: ("exited", 0)) == 0
    assert seen == [(True, True, False)]
    assert res.read_text().startswith("EXITED pid=42 code=0")
    assert list(tmp_path.glob("*.tmp")) == []
    assert capsys.readouterr().out.count("\n") == 1


def test_inline_without_result_writes_nothing(tmp_path):
    assert mod.main(["42"], waiter=lambda p, i, t: ("exited", 0)) == 0
    assert list(tmp_path.rglob("*.result")) == []


def test_strict_refusal_exits_5(session, tmp_path, capsys, monkeypatch):
    def boom(self, cmd, **kw):
        raise OSError("breakaway denied")

    monkeypatch.setattr(session, "__init__", boom)
    assert mod.main(["42", "--result", str(tmp_path / "w.result")]) == 5
    cap = capsys.readouterr()
    assert cap.out == "" and "cannot leave the Claude Code session's job" in cap.err


def test_marker_set_skips_relaunch(session, monkeypatch):
    monkeypatch.setenv("COORDINATOR_GUARD_DETACHED", "1")
    assert mod.main(["42"], waiter=lambda p, i, t: ("exited", 0)) == 0
    assert session.spawned == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-time gate")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_session_half_under_500ms_cpu(tmp_path):
    pt = importlib.util.spec_from_file_location(
        "box_memory_cli_process_time", Path(__file__).with_name("test_box_memory_cli_process_time.py")
    )
    ptm = importlib.util.module_from_spec(pt)
    pt.loader.exec_module(ptm)
    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    env = {**os.environ, "COORDINATOR_GUARD_FAKE_SESSION": "1"}
    env.pop("COORDINATOR_GUARD_DETACHED", None)
    proc = subprocess.Popen(
        [sys.executable, str(TOOL), str(target.pid), "--timeout", "2", "--result", str(tmp_path / "w.result")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    detached = None
    try:
        out, err = proc.communicate(timeout=60)
        text = err.decode(errors="replace")
        assert proc.returncode in (0, 1), text
        if "DETACHED" in text:
            detached = int(text.split("pid=")[1].split()[0])
        assert ptm._cpu_seconds(proc) < 0.5
    finally:
        target.kill()
        if detached:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(detached)],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )


def test_exited_line(capsys):
    assert mod.main(["42"], waiter=lambda p, i, t: ("exited", 3)) == 0
    out = capsys.readouterr().out
    assert out.startswith("EXITED pid=42 code=3 elapsed=") and out.endswith("s\n")
    assert out.count("\n") == 1


def test_exited_unknown_code(capsys):
    assert mod.main(["42"], waiter=lambda p, i, t: ("exited", None)) == 0
    assert "code=?" in capsys.readouterr().out


def test_timeout(capsys):
    assert mod.main(["7", "--timeout", "1"], waiter=lambda p, i, t: ("timeout", None)) == 1
    assert capsys.readouterr().out == "TIMEOUT pid=7\n"


def test_missing_pid(capsys):
    assert mod.main(["9"], waiter=lambda p, i, t: ("nopid", None)) == 1
    assert capsys.readouterr().out == "NO-SUCH-PID pid=9\n"


def test_poll_branch_exits_after_alive_then_gone():
    calls = []

    def kill(pid, sig):
        calls.append((pid, sig))
        if len(calls) >= 3:
            raise ProcessLookupError

    state, code = mod.poll_waiter(5, 0.0, None, kill=kill, sleep=lambda s: None)
    assert (state, code) == ("exited", None) and calls[0] == (5, 0)


def test_poll_branch_missing_at_start():
    def kill(pid, sig):
        raise ProcessLookupError

    assert mod.poll_waiter(5, 0.0, None, kill=kill, sleep=lambda s: None)[0] == "nopid"


def test_poll_branch_timeout():
    t = [0.0]

    def sleep(s):
        t[0] += s

    state, _ = mod.poll_waiter(
        5, 1.0, 3.0, kill=lambda p, s: None, sleep=sleep, clock=lambda: t[0]
    )
    assert state == "timeout"


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        mod.main(["--help"])
    assert e.value.code == 0 and "PID" in capsys.readouterr().out.upper()


def test_windows_without_psutil_refuses_instead_of_signalling(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(mod.os, "name", "nt")
    with pytest.raises(SystemExit) as e:
        mod.default_waiter()
    assert "psutil is required" in str(e.value)


def test_posix_without_psutil_falls_back_to_poll(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(mod.os, "name", "posix")
    assert mod.default_waiter() is mod.poll_waiter
