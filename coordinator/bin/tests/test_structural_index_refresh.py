"""structural-index-refresh: every path must stay non-blocking and exit 0."""
import importlib.util
import json
import subprocess
import sys
import types
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


def _admission(monkeypatch, avail_mb, trusted=True):
    from coordinator_core.ops.ceremony import post_close_admission as adm
    from coordinator_core.group_em import admission_post

    posts = []
    monkeypatch.setattr(adm, "read_available", lambda: (avail_mb, trusted))
    monkeypatch.setattr(
        admission_post, "post", lambda kind, **kw: posts.append((kind, kw)) or {"posted": True}
    )
    return posts


def test_spawn_ensure_launches_detached_wrapper_not_ensure(tmp_path, monkeypatch):
    posts = _admission(monkeypatch, 100000)
    captured = {}
    monkeypatch.setattr(
        sir.subprocess, "Popen", lambda cmd, **kw: captured.update(cmd=cmd, kw=kw) or FakePid()
    )
    monkeypatch.setattr(sir, "lower_io_priority_pid", lambda pid: None)
    sir.spawn_ensure(Path("/rag"), tmp_path)
    assert "--run-and-record" in captured["cmd"] and "ensure" not in captured["cmd"]
    assert captured["kw"]["stdout"] == subprocess.DEVNULL
    assert ("creationflags" in captured["kw"]) or captured["kw"].get("start_new_session")
    assert [p[0] for p in posts] == ["START"]


def test_spawn_ensure_admitted_child_is_below_normal(tmp_path, monkeypatch):
    _admission(monkeypatch, 100000)
    captured = {}
    monkeypatch.setattr(sir.subprocess, "Popen", lambda cmd, **kw: captured.update(kw=kw) or FakePid())
    monkeypatch.setattr(sir, "lower_io_priority_pid", lambda pid: None)
    monkeypatch.setattr(sir, "lower_io_priority_self", lambda: None)
    monkeypatch.setattr(sir.subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)
    monkeypatch.setattr(sir.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(sir.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    niced = []
    fake_os = types.SimpleNamespace(name="nt", nice=niced.append)
    monkeypatch.setattr(sir, "os", fake_os)
    sir.spawn_ensure(Path("/rag"), tmp_path)
    assert captured["kw"]["creationflags"] & 0x4000
    fake_os.name = "posix"
    sir.spawn_ensure(Path("/rag"), tmp_path)
    captured["kw"]["preexec_fn"]()
    assert niced == [sir.POSIX_NICE]


class FakePid:
    pid = 4242


def test_start_notice_follows_popen_and_carries_pid(tmp_path, monkeypatch):
    posts = _admission(monkeypatch, 100000)
    order = []
    monkeypatch.setattr(sir, "lower_io_priority_pid", lambda pid: None)

    def popen(cmd, **kw):
        order.append(("popen", len(posts)))
        return FakePid()

    monkeypatch.setattr(sir.subprocess, "Popen", popen)
    sir.spawn_ensure(Path("/rag"), tmp_path)
    assert order == [("popen", 0)]
    assert len(posts) == 1
    kind, kw = posts[0]
    assert kind == "START" and kw["pids"] == [4242] and "peak" in kw["note"]


def test_failed_popen_posts_nothing(tmp_path, monkeypatch):
    posts = _admission(monkeypatch, 100000)

    def popen(*a, **k):
        raise OSError("no exe")

    monkeypatch.setattr(sir.subprocess, "Popen", popen)
    with pytest.raises(OSError):
        sir.spawn_ensure(Path("/rag"), tmp_path)
    assert posts == []


class FakeLibc:
    def __init__(self):
        self.calls = []

    def syscall(self, *a):
        self.calls.append(("syscall", a))

    def setiopolicy_np(self, *a):
        self.calls.append(("setiopolicy_np", a))


def test_io_priority_linux_sets_idle_class(monkeypatch):
    libc = FakeLibc()
    monkeypatch.setattr(sir, "_platform", lambda: "linux")
    monkeypatch.setattr(sir, "_libc", lambda: libc)
    import platform

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    sir.lower_io_priority_self()
    assert libc.calls == [("syscall", (251, 1, 0, 3 << 13))]


def test_io_priority_macos_sets_throttle(monkeypatch):
    libc = FakeLibc()
    monkeypatch.setattr(sir, "_platform", lambda: "darwin")
    monkeypatch.setattr(sir, "_libc", lambda: libc)
    sir.lower_io_priority_self()
    assert libc.calls == [("setiopolicy_np", (1, 0, 3))]


def test_io_priority_failure_is_swallowed(monkeypatch):
    def boom():
        raise OSError("no libc")

    monkeypatch.setattr(sir, "_platform", lambda: "darwin")
    monkeypatch.setattr(sir, "_libc", boom)
    sir.lower_io_priority_self()
    libc = FakeLibc()
    monkeypatch.setattr(sir, "_libc", lambda: libc)
    monkeypatch.setattr(sir, "_platform", lambda: "win32")
    sir.lower_io_priority_self()
    assert libc.calls == []


def test_io_priority_windows_uses_psutil_and_skips_without_it(monkeypatch):
    seen = []

    class Proc:
        def __init__(self, pid):
            seen.append(pid)

        def ionice(self, level):
            seen.append(level)

    fake = types.SimpleNamespace(Process=Proc, IOPRIO_VERYLOW=0)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    sir.lower_io_priority_pid(77)
    assert seen == [77, 0]
    monkeypatch.setitem(sys.modules, "psutil", None)  # ImportError on import
    sir.lower_io_priority_pid(77)
    assert seen == [77, 0]


def test_spawn_ensure_applies_windows_io_priority_after_popen(tmp_path, monkeypatch):
    _admission(monkeypatch, 100000)
    monkeypatch.setattr(sir.subprocess, "Popen", lambda cmd, **kw: FakePid())
    for name, val in (("BELOW_NORMAL_PRIORITY_CLASS", 0x4000), ("CREATE_NO_WINDOW", 0x08000000),
                      ("CREATE_NEW_PROCESS_GROUP", 0x200)):
        monkeypatch.setattr(sir.subprocess, name, val, raising=False)
    monkeypatch.setattr(sir, "os", types.SimpleNamespace(name="nt"))
    pids = []
    monkeypatch.setattr(sir, "lower_io_priority_pid", pids.append)
    sir.spawn_ensure(Path("/rag"), tmp_path)
    assert pids == [4242]


def test_spawn_ensure_defers_and_records_when_memory_short(tmp_path, monkeypatch):
    posts = _admission(monkeypatch, 9000)  # 2048 + 8192 > 9000
    monkeypatch.setattr(
        sir.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned despite deferral")
    )
    assert sir.spawn_ensure(Path("/rag"), tmp_path) is None
    rec = json.loads((tmp_path / ".structural-index" / "refresh-last.json").read_text(encoding="utf-8"))
    assert "need 10240MB > avail 9000MB" in rec["deferred"]
    assert posts == []


def test_unreadable_memory_defers_and_refresh_reports(tmp_path, monkeypatch):
    _admission(monkeypatch, None, trusted=False)
    monkeypatch.setattr(sir, "resolve_index_repo", lambda: Path("/rag"))
    monkeypatch.setattr(
        sir.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned despite deferral")
    )
    assert "deferred" in sir.refresh(tmp_path, 1, env={})


def test_main_exits_zero_on_unexpected_error(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sir, "refresh", boom)
    assert sir.main(["--root", str(tmp_path)]) == 0
    assert "skipped" in capsys.readouterr().out
