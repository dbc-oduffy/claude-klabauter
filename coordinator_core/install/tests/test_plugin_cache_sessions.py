"""plugin_cache_sessions: binding write, live-session detection, relaunch lines, prune."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.install import plugin_cache_sessions as pcs
from coordinator_core.session import core

psutil = pytest.importorskip("psutil")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    return tmp_path


def _own_start() -> float:
    return psutil.Process(os.getpid()).create_time()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _register(home: Path, sid: str, *, alive: bool = True, name: str = "proj-1") -> None:
    start = _own_start() if alive else _own_start() - 3600
    (home / "sessions" / f"{sid}.json").write_text(
        json.dumps(
            {"sessionId": sid, "pid": os.getpid(), "procStart": _iso(start), "name": name}
        ),
        encoding="utf-8",
    )


def _since(home: Path, epoch: float) -> None:
    d = pcs.bindings_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    (d / ".since").write_text(f"{epoch}\n", encoding="utf-8")


def test_bound_session_returned(home, tmp_path):
    cache = tmp_path / "plugins" / "cache" / "mk" / "pl"
    (cache / "1.0").mkdir(parents=True)
    _register(home, "s1")
    pcs.write_binding("s1", str(cache / "1.0"), claude_home=home)
    got = pcs.sessions_bound_to(cache)
    assert [(s.session_id, s.basis, s.name) for s in got] == [("s1", "bound", "proj-1")]


def test_plugin_dir_binding_not_returned(home, tmp_path):
    cache = tmp_path / "plugins" / "cache" / "mk" / "pl"
    cache.mkdir(parents=True)
    _register(home, "s1")
    pcs.write_binding("s1", str(tmp_path / "elsewhere" / "plugin"), claude_home=home)
    assert pcs.sessions_bound_to(cache) == []


def test_sibling_prefix_is_not_a_descendant(home, tmp_path):
    cache = tmp_path / "cache" / "pl"
    cache.mkdir(parents=True)
    _register(home, "s1")
    pcs.write_binding("s1", str(tmp_path / "cache" / "pl-other"), claude_home=home)
    assert pcs.sessions_bound_to(cache) == []


def test_unbound_session_predating_since_returned(home, tmp_path):
    _register(home, "s1")
    _since(home, _own_start() + 10)
    got = pcs.sessions_bound_to(tmp_path / "cache")
    assert [(s.session_id, s.basis) for s in got] == [("s1", "predates-bindings")]


def test_unbound_session_without_bindings_dir_returned(home, tmp_path):
    _register(home, "s1")
    got = pcs.sessions_bound_to(tmp_path / "cache")
    assert [s.basis for s in got] == ["predates-bindings"]


def test_unbound_session_after_since_not_returned(home, tmp_path):
    _register(home, "s1")
    _since(home, _own_start() - 100)
    assert pcs.sessions_bound_to(tmp_path / "cache") == []


def test_dead_pid_not_returned(home, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _register(home, "s1", alive=False)
    pcs.write_binding("s1", str(cache), claude_home=home)
    assert pcs.sessions_bound_to(cache) == []


def test_liveness_unknown_when_psutil_absent(home, tmp_path, monkeypatch):
    _register(home, "s1")
    monkeypatch.setattr(core, "_psutil", lambda: None)
    got = pcs.sessions_bound_to(tmp_path / "cache")
    assert [(s.session_id, s.basis) for s in got] == [("s1", "liveness-unknown")]


def test_unexpected_error_returns_sentinel(home, tmp_path, monkeypatch):
    _register(home, "s1")

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(core, "stable_pid_alive", boom)
    got = pcs.sessions_bound_to(tmp_path / "cache")
    assert [s.basis for s in got] == ["liveness-unknown"]


def test_write_binding_idempotent(home, tmp_path):
    pcs.write_binding("s1", str(tmp_path / "root"), claude_home=home)
    target = pcs.bindings_dir(home) / "s1"
    assert target.read_text(encoding="utf-8") == (tmp_path / "root").as_posix() + "\n"
    since = (pcs.bindings_dir(home) / ".since").read_text(encoding="utf-8")
    os.utime(target, (1, 1))
    pcs.write_binding("s1", str(tmp_path / "root"), claude_home=home)
    assert target.stat().st_mtime == 1
    assert (pcs.bindings_dir(home) / ".since").read_text(encoding="utf-8") == since


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "..", "", ".since"])
def test_write_binding_refuses_traversal_id(home, tmp_path, bad):
    pcs.write_binding(bad, str(tmp_path / "root"), claude_home=home)
    d = pcs.bindings_dir(home)
    assert not d.exists() or [p.name for p in d.iterdir() if p.name != ".since"] == []
    assert not (tmp_path / "x").exists()


def test_write_binding_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    pcs.write_binding("s1", "/r", claude_home=blocker)


def test_relaunch_lines(tmp_path):
    assert pcs.relaunch_lines(tmp_path, [], step="dev-sync") == []
    sessions = [
        pcs.BoundSession("sid-a", "alpha-1", 11, 0.0, "/r", "bound"),
        pcs.BoundSession("sid-b", None, 22, 0.0, None, "predates-bindings"),
    ]
    lines = pcs.relaunch_lines(tmp_path, sessions, step="dev-sync")
    assert lines[0].startswith("2 live session(s)") and tmp_path.as_posix() in lines[0]
    assert "alpha-1" in lines[1] and "11" in lines[1] and "sid-a" in lines[1] and "bound" in lines[1]
    assert "sid-b" in lines[2] and "predates-bindings" in lines[2]
    assert lines[-1] == "relaunch these sessions, then re-run dev-sync"


def test_prune_removes_only_old_unregistered(home):
    d = pcs.bindings_dir(home)
    pcs.write_binding("old-dead", "/r", claude_home=home)
    pcs.write_binding("new-dead", "/r", claude_home=home)
    pcs.write_binding("old-live", "/r", claude_home=home)
    _register(home, "old-live")
    old = time.time() - 200000
    for n in ("old-dead", "old-live"):
        os.utime(d / n, (old, old))
    assert pcs.prune_dead_bindings(claude_home=home) == 1
    assert sorted(p.name for p in d.iterdir()) == [".since", "new-dead", "old-live"]


def test_sixty_records_no_spawn_under_budget(home, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(60):
        _register(home, f"sess-{i}", alive=i % 2 == 0)
        if i % 3 == 0:
            pcs.write_binding(f"sess-{i}", str(cache), claude_home=home)

    def no_spawn(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    pcs.sessions_bound_to(cache)  # warm imports and psutil
    t0 = time.process_time()
    got = pcs.sessions_bound_to(cache)
    elapsed = time.process_time() - t0
    assert got and all(s.basis != "liveness-unknown" for s in got)
    assert elapsed < 0.05


def test_module_has_no_process_enumeration():
    src = Path(pcs.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in src and "process_iter" not in src
