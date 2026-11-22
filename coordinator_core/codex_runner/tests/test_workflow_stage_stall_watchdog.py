"""A Codex stage whose stream is silent past the stall window is killed and retried."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.codex_runner import workflow as agents


@pytest.fixture
def stage_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "01a1b63b-acfb-78d1-86f2-a74ad6ca4572")
    host = SimpleNamespace(admission_file=lambda: "unused", read_recorded_peak=lambda *_a: None)
    monkeypatch.setattr(agents.capacity, "_load_box_memory_host", lambda: host)
    monkeypatch.setattr(agents.capacity, "admit_before_spawn",
                        lambda key, peak, **_kw: SimpleNamespace(admitted=True, peak_gib=peak, reason="ok"))
    monkeypatch.setattr(agents.capacity, "release_spawn", lambda *a, **kw: None)
    monkeypatch.setattr(agents.slot_grant, "check_slot_grant", lambda *a, **kw: (False, None))
    monkeypatch.setattr(agents.admission_post, "post", lambda *a, **kw: {"delivered": True})
    monkeypatch.setattr(agents.admission_post, "record_undelivered", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "preflight_auth", lambda **_kw: None)
    monkeypatch.setattr(agents, "_STALL_POLL_S", 0.05)

    def install(silent_attempts: int) -> Path:
        marker = tmp_path / "attempts"
        script = tmp_path / "cli.py"
        script.write_text(
            "import sys,time,json,pathlib\n"
            f"m=pathlib.Path({str(marker)!r})\n"
            "n=int(m.read_text()) if m.exists() else 0\n"
            "m.write_text(str(n+1))\n"
            "args=sys.argv[1:]\n"
            "sys.stdin.read()\n"
            f"if n<{silent_attempts}:\n"
            "    time.sleep(60)\n"
            "open(args[args.index('-o')+1],'w',encoding='utf-8').write('{\"ok\":true}')\n"
            "print(json.dumps({'type':'turn.completed'}),flush=True)\n", encoding="utf-8")
        monkeypatch.setattr(agents, "resolve_codex", lambda **_kw: [sys.executable, str(script)])
        return marker

    return tmp_path, install


def test_silent_stage_is_killed_and_retried(stage_env) -> None:
    tmp_path, install = stage_env
    marker = install(silent_attempts=1)
    value = agents.run_agent("x", {"stallWindowS": 0.5}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert value == '{"ok":true}'
    assert marker.read_text() == "2"


def test_stage_silent_on_every_attempt_fails_stalled(stage_env) -> None:
    tmp_path, install = stage_env
    marker = install(silent_attempts=99)
    with pytest.raises(agents.StageStalled):
        agents.run_agent("x", {"stallWindowS": 0.5}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert marker.read_text() == str(agents.STALL_RETRIES + 1)


def test_stall_kills_descendants(tmp_path: Path) -> None:
    import subprocess
    import time

    import psutil

    parent = subprocess.Popen([sys.executable, "-c",
                               "import subprocess,sys,time;"
                               "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)"])
    try:
        deadline = time.monotonic() + 10
        while not psutil.Process(parent.pid).children() and time.monotonic() < deadline:
            time.sleep(0.05)
        kids = psutil.Process(parent.pid).children(recursive=True)
        assert kids
        agents._kill_descendants(parent.pid)
        psutil.wait_procs(kids, timeout=5)
        assert not any(k.is_running() for k in kids)
        assert parent.poll() is None
    finally:
        parent.kill()
        parent.wait()


def test_speaking_stage_is_not_retried(stage_env) -> None:
    tmp_path, install = stage_env
    marker = install(silent_attempts=0)
    agents.run_agent("x", {"stallWindowS": 0.5}, repo_root=tmp_path, artifact_dir=tmp_path / "agent")
    assert marker.read_text() == "1"
