"""structural-index-refresh: every path must stay non-blocking and exit 0."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "structural-index-refresh.py"
_spec = importlib.util.spec_from_file_location("structural_index_refresh_cli", _SRC)
sir = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sir)


class FakeProc:
    def __init__(self, rc=0, timeout=False):
        self.rc, self.timeout = rc, timeout

    def wait(self, timeout=None):
        if self.timeout:
            raise subprocess.TimeoutExpired("ensure", timeout)
        return self.rc


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def fake(rag, root):
        calls.append((rag, root))
        return FakeProc(rc=state.rc, timeout=state.timeout)

    class State:
        rc = 0
        timeout = False

    state = State()
    monkeypatch.setattr(sir, "spawn_ensure", fake)
    monkeypatch.setattr(sir, "resolve_index_repo", lambda: Path("/rag"))
    return state, calls


def _status(root, **fields):
    d = root / ".structural-index"
    d.mkdir(exist_ok=True)
    (d / "status.json").write_text(json.dumps(fields), encoding="utf-8")


def test_registry_missing_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(sir, "resolve_index_repo", lambda: None)
    called = []
    monkeypatch.setattr(sir, "spawn_ensure", lambda *a: called.append(a))
    assert sir.refresh(tmp_path, 1, env={}) is None
    assert not called
    assert sir.main(["--root", str(tmp_path)]) == 0


def test_flag_off_skips_everything(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("registry consulted")

    monkeypatch.setattr(sir, "resolve_index_repo", boom)
    assert sir.refresh(tmp_path, 1, env={sir.KILL_SWITCH: "1"}) is None


def test_cold_build_goes_to_background_without_waiting(tmp_path, spawned):
    state, calls = spawned
    state.timeout = True  # would surface as a timeout line if waited on
    assert "cold build started" in sir.refresh(tmp_path, 1, env={})
    assert len(calls) == 1


def test_full_rebuild_required_goes_to_background(tmp_path, spawned):
    _status(tmp_path, state="stale", full_rebuild_required=True, reason="toolchain_changed")
    state, _ = spawned
    state.timeout = True
    assert "cold build started" in sir.refresh(tmp_path, 1, env={})


def test_timeout_leaves_ensure_running(tmp_path, spawned):
    state, _ = spawned
    state.timeout = True
    _status(tmp_path, state="stale", full_rebuild_required=False)
    assert "still running in background" in sir.refresh(tmp_path, 5, env={})


def test_noop_is_silent_and_incremental_reports(tmp_path, spawned):
    _status(tmp_path, state="fresh", full_rebuild_required=False, refresh={"mode": "noop"})
    assert sir.refresh(tmp_path, 1, env={}) is None
    _status(tmp_path, full_rebuild_required=False, refresh={"mode": "incremental"})
    assert "incremental" in sir.refresh(tmp_path, 1, env={})


def test_ensure_failure_reported_not_raised(tmp_path, spawned):
    state, _ = spawned
    state.rc = 4
    _status(tmp_path, full_rebuild_required=False)
    assert "exited 4" in sir.refresh(tmp_path, 1, env={})


def test_already_running_is_silent(tmp_path, spawned):
    state, _ = spawned
    state.rc = sir.ALREADY_RUNNING
    _status(tmp_path, full_rebuild_required=False)
    assert sir.refresh(tmp_path, 1, env={}) is None


def _fake_run(rc, stderr):
    def run(cmd, **kw):
        assert kw["stderr"] == subprocess.PIPE
        return subprocess.CompletedProcess(cmd, rc, stdout=None, stderr=stderr)

    return run


def test_run_and_record_writes_failure_record(tmp_path, monkeypatch):
    err = "\n".join(f"line{i}" for i in range(8)) + "\nsqlite3.OperationalError: database is locked\n"
    monkeypatch.setattr(sir.subprocess, "run", _fake_run(1, err))
    assert sir.run_and_record(Path("/rag"), tmp_path) == 1
    rec = json.loads((tmp_path / ".structural-index" / "refresh-last.json").read_text(encoding="utf-8"))
    assert rec["exit_code"] == 1
    assert len(rec["stderr_tail"]) == 5
    assert rec["stderr_tail"][-1].endswith("database is locked")
    assert rec["started_at"] and rec["finished_at"]


def test_run_and_record_success_and_spawn_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sir.subprocess, "run", _fake_run(0, ""))
    sir.run_and_record(Path("/rag"), tmp_path)
    path = tmp_path / ".structural-index" / "refresh-last.json"
    assert json.loads(path.read_text(encoding="utf-8"))["exit_code"] == 0

    def boom(*a, **k):
        raise OSError("no exe")

    monkeypatch.setattr(sir.subprocess, "run", boom)
    assert sir.run_and_record(Path("/rag"), tmp_path) == -1
    assert "no exe" in json.loads(path.read_text(encoding="utf-8"))["stderr_tail"][-1]


def test_hidden_mode_dispatches_wrapper(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(sir, "run_and_record", lambda rag, root: seen.append((rag, root)) or 7)
    assert sir.main(["--run-and-record", "--root", str(tmp_path), "--rag", "/rag"]) == 0
    assert seen == [(Path("/rag"), tmp_path.resolve())]


def test_spawn_ensure_launches_detached_wrapper_not_ensure(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(sir.subprocess, "Popen", lambda cmd, **kw: captured.update(cmd=cmd, kw=kw))
    sir.spawn_ensure(Path("/rag"), tmp_path)
    assert "--run-and-record" in captured["cmd"] and "ensure" not in captured["cmd"]
    assert captured["kw"]["stdout"] == subprocess.DEVNULL
    assert ("creationflags" in captured["kw"]) or captured["kw"].get("start_new_session")


def test_main_exits_zero_on_unexpected_error(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sir, "refresh", boom)
    assert sir.main(["--root", str(tmp_path)]) == 0
    assert "skipped" in capsys.readouterr().out
