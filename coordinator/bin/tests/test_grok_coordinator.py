"""grok-coordinator launches grok as a child and does not shadow a bare grok."""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "grok-coordinator.py"
spec = importlib.util.spec_from_file_location("grok_coordinator", BIN)
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def test_interactive_argv_skips_permissions_and_does_not_resume():
    from coordinator_core.agent_pipe.model_tiers import GROK_EM_EFFORT, model_for_role

    argv = gc.interactive_argv("grok", ["--cwd", "/tmp/work"])
    assert argv[:2] == ["grok", "--dangerously-skip-permissions"]
    assert "-p" not in argv and "-r" not in argv
    assert argv[-2:] == ["--cwd", "/tmp/work"]
    assert argv[argv.index("--model") + 1] == model_for_role("grok", "em")
    assert argv[argv.index("--reasoning-effort") + 1] == GROK_EM_EFFORT


def test_interactive_argv_keeps_a_caller_model():
    argv = gc.interactive_argv("grok", ["--model", "grok-4.5", "--cwd", "/tmp/work"])
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "grok-4.5"


def test_headless_argv_is_one_turn_without_resume():
    from coordinator_core.agent_pipe.model_tiers import model_for_role

    argv = gc.headless_argv("grok", "/work", "hello")
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("-p") + 1] == "hello"
    assert argv[argv.index("--cwd") + 1] == "/work"
    assert argv[argv.index("--model") + 1] == model_for_role("grok", "em")
    assert "-r" not in argv and "--fork-session" not in argv


def test_strip_coordinator_is_not_forwarded():
    assert gc.strip_coordinator(["--coordinator", "--cwd", "/tmp/work"]) == ["--cwd", "/tmp/work"]
    assert "--coordinator" not in gc.interactive_argv("grok", gc.strip_coordinator(["--coordinator"]))


def test_stop_hook_file_names_the_script(tmp_path):
    path = gc.ensure_stop_hook(tmp_path, command="/engine/bin/unimatrix-grok-stop.py")
    body = path.read_text(encoding="utf-8")
    assert path.parent.name == "hooks"
    assert "/engine/bin/unimatrix-grok-stop.py" in body
    assert "Stop" in body


@pytest.mark.skipif(os.name != "nt", reason="Grok runs Windows hooks through PowerShell")
def test_windows_stop_hook_runs_without_a_py_file_association(tmp_path):
    path = gc.ensure_stop_hook(tmp_path)
    command = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert command.endswith(".cmd") and '"' not in command
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("no PowerShell on this box")
    # Spawn it as Grok does: the command string through PowerShell, the hook payload on stdin.
    proc = subprocess.run([shell, "-NoProfile", "-Command", command], input="{}",
                          capture_output=True, text=True, timeout=60)
    assert "file type" not in (proc.stdout + proc.stderr)
    assert proc.returncode == 0, proc.stderr


def test_tmp_sessions_sweep_removes_dead_empty_dirs_only(tmp_path):
    root = tmp_path / "tmp" / "sessions"
    (root / "dead" / "sub").mkdir(parents=True)
    (root / "live").mkdir()
    (root / "has-file").mkdir()
    (root / "has-file" / "keep.txt").write_text("x")
    assert gc.sweep_tmp_sessions(tmp_path / "work", {"live"}, drive_root=tmp_path) == 1
    assert not (root / "dead").exists()
    assert (root / "live").is_dir() and (root / "has-file" / "keep.txt").is_file()


def test_tmp_sessions_sweep_removes_the_emptied_tmp_root(tmp_path):
    (tmp_path / "tmp" / "sessions" / "dead").mkdir(parents=True)
    gc.sweep_tmp_sessions(tmp_path, set(), drive_root=tmp_path)
    assert not (tmp_path / "tmp").exists()


def test_register_host_binding_adopts_the_session_through_the_engine(monkeypatch):
    from coordinator_core.session import external_registry

    seen = []
    monkeypatch.setattr(external_registry, "adopt_grok_session",
                        lambda sid, env=None, *a, **k: seen.append((sid, env)) or {"role": "em"})
    sid = "01a21334-b3fa-70b0-b317-abd3a378a142"
    assert gc.register_host_binding(sid) is True
    assert seen == [(sid, {"GROK_SESSION_ID": sid})]


def test_register_host_binding_is_false_without_evidence(monkeypatch):
    from coordinator_core.session import external_registry

    monkeypatch.setattr(external_registry, "adopt_grok_session", lambda *a, **k: None)
    assert gc.register_host_binding("01a21334-b3fa-70b0-b317-abd3a378a142") is False


def test_run_interactive_binds_the_child_to_a_kill_on_close_job(monkeypatch, tmp_path):
    bound = []
    child = type("C", (), {"pid": 4242, "wait": lambda self: 3})()
    monkeypatch.setattr(gc, "load_grok_adapter", lambda: type("M", (), {"grok_home": staticmethod(lambda: tmp_path / "home")}))
    monkeypatch.setattr(gc, "grok_binary", lambda: "bin/grok.exe")
    monkeypatch.setattr(gc, "_hold_inbox", lambda m, pid, stop: None)
    monkeypatch.setattr(gc.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(gc, "bind_kill_on_close", lambda pid: bound.append(pid))

    assert gc.run_interactive([]) == 3
    assert bound == [4242]


@pytest.mark.skipif(os.name != "nt", reason="job objects are Windows-only")
def test_grok_child_dies_when_its_python_parent_is_killed(tmp_path):
    """A hidden parent binds a hidden sleeper; killing the parent must take the sleeper with it."""
    import subprocess
    import sys
    import time

    hidden = subprocess.CREATE_NO_WINDOW
    script = tmp_path / "parent.py"
    script.write_text(
        "import importlib.util, subprocess, sys, time\n"
        f"s = importlib.util.spec_from_file_location('g', {str(BIN)!r})\n"
        "g = importlib.util.module_from_spec(s); s.loader.exec_module(g)\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=%d)\n"
        "job = g.bind_kill_on_close(c.pid)\n"
        "print(c.pid, job is not None, flush=True)\n"
        "time.sleep(60)\n" % hidden,
        encoding="utf-8",
    )
    parent = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True, creationflags=hidden)
    child_pid = None
    try:
        pid_text, bound = parent.stdout.readline().split()
        child_pid = int(pid_text)
        assert bound == "True"
        parent.kill()
        parent.wait()
        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(child_pid):
            time.sleep(0.1)
        assert not _pid_alive(child_pid)
    finally:
        parent.kill()
        if child_pid and _pid_alive(child_pid):
            subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], capture_output=True, creationflags=hidden)


def _pid_alive(pid: int) -> bool:
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    handle = k32.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def test_session_for_pid_matches_only_the_child():
    rows = [{"session_id": "aaa", "pid": 5}, {"session_id": "bbb", "pid": 9}]
    assert gc.session_for_pid(rows, 9)["session_id"] == "bbb"
    assert gc.session_for_pid(rows, 3) is None
    assert gc.session_for_pid({"nope": True}, 5) is None
