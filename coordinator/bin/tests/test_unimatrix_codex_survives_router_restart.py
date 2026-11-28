"""Two live Codex EM faces stay listed after a router restart leaves their peers.json rows pid-less."""
import importlib.util
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
CWD = "C:/dev/claude-klabauter"
EMS = {
    "coordinator-content-repo-codex-machine-b-lo-127": ("019a0000-1270-7000-8000-000000000127", 424227),
    "claude-klabauter-codex-machine-b-lo-126": ("019a0000-1260-7000-8000-000000000126", 424226),
}
AGED_MS = 3 * 60 * 60 * 1000


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RestartedBox:
    """A tmp_path box after a router restart: pid-less Codex rows, aged faces, no writer-lock holder."""

    def __init__(self, tmp_path, monkeypatch, live_pids):
        self.roster = _load("unimatrix-roster.py", "unimatrix_roster_restart")
        self.sessions = tmp_path / "sessions"
        self.sessions.mkdir()
        home = tmp_path / "home"
        (home / "unimatrix").mkdir(parents=True)
        self.codex_home = tmp_path / "codex"
        self.codex_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
        monkeypatch.setenv("CODEX_HOME", str(self.codex_home))
        liveness = self.roster._liveness()
        monkeypatch.setattr(liveness, "entry_live", lambda d: isinstance(d, dict) and d.get("pid") == os.getpid())
        monkeypatch.setattr(liveness, "pid_alive", lambda pid: pid in live_pids or pid == os.getpid())
        monkeypatch.setattr(liveness, "proc_start", lambda pid: f"start-{pid}")
        monkeypatch.setattr(liveness, "_process_argv", lambda pid: ["codex"])
        monkeypatch.setattr(liveness, "agy_presence_held", lambda conversation, presence_dir=None: False)
        monkeypatch.setattr(liveness, "lock_holder_pids", lambda path: [])
        started = int(time.time() * 1000) - AGED_MS
        faces = [{"name": n, "vendor": "codex", "sessionId": f"peer-{n}", "pid": os.getpid(), "cwd": CWD,
                  "target": t, "unimatrixRouterPid": os.getpid(), "startedAt": started}
                 for n, (t, _) in EMS.items()]
        peers = [{"name": n, "vendor": "codex", "target": t, "cwd": CWD} for n, (t, _) in EMS.items()]
        for face in faces:
            (self.sessions / f"{face['name']}.json").write_text(json.dumps(face), encoding="utf-8")
        (home / "unimatrix" / "peers.json").write_text(json.dumps(peers), encoding="utf-8")

    def log_codex_processes(self, pids):
        """Record, in the Codex logs db, the process that last wrote each thread."""
        with closing(sqlite3.connect(self.codex_home / "logs_1.sqlite")) as conn:
            conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                         "ts_nanos INTEGER NOT NULL, thread_id TEXT, process_uuid TEXT)")
            conn.executemany("INSERT INTO logs (ts, ts_nanos, thread_id, process_uuid) VALUES (?,?,?,?)",
                             [(int(time.time()), 0, t, f"pid:{pids[n]}:x") for n, (t, _) in EMS.items()])
            conn.commit()

    def listed(self):
        """Names the listing shows: every sessions-dir entry through the default `listed_row`."""
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.sessions.glob("*.json"))]
        return sorted(r["name"] for r in rows if self.roster.listed_row(r, rows))


def test_both_live_codex_ems_are_listed_after_a_router_restart(tmp_path, monkeypatch):
    box = RestartedBox(tmp_path, monkeypatch, live_pids={p for _, p in EMS.values()})
    box.log_codex_processes({n: p for n, (_, p) in EMS.items()})
    assert box.listed() == sorted(EMS)


def test_a_codex_em_whose_process_is_dead_is_still_dropped_after_a_router_restart(tmp_path, monkeypatch):
    live, dead = sorted(EMS)
    box = RestartedBox(tmp_path, monkeypatch, live_pids={EMS[live][1]})
    box.log_codex_processes({live: EMS[live][1], dead: EMS[dead][1]})
    assert box.listed() == [live]


def test_a_codex_em_with_no_recorded_process_anywhere_is_still_dropped_once_its_grace_lapses(tmp_path, monkeypatch):
    box = RestartedBox(tmp_path, monkeypatch, live_pids={p for _, p in EMS.values()})
    assert box.listed() == []


THREAD = "019a0000-1260-7000-8000-000000000126"
ADDRESS = {"schema_version": 1, "provider": "codex", "session_id": THREAD, "address_version": 1,
           "name": "claude-klabauter-codex-machine-b-lo-126"}


def _hook_box(tmp_path, monkeypatch):
    """A tmp_path box whose Codex logs db names pid 424226 as the writer of THREAD, with an engine address on disk."""
    import socket
    hook = _load("unimatrix-codex-hook-sessionstart.py", "unimatrix_codex_hook_restart")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    with closing(sqlite3.connect(codex_home / "logs_1.sqlite")) as conn:
        conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
                     "ts_nanos INTEGER NOT NULL, thread_id TEXT, process_uuid TEXT)")
        conn.execute("INSERT INTO logs (ts, ts_nanos, thread_id, process_uuid) VALUES (?,?,?,?)",
                     (int(time.time()), 0, THREAD, "pid:424226:x"))
        conn.commit()
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / f"{THREAD}.json").write_text(
        json.dumps({**ADDRESS, "box": socket.gethostname()}), encoding="utf-8")
    monkeypatch.setattr(hook, "codex_holder", lambda pid=None: {"codexPid": os.getpid()})
    return hook, registry


def test_the_hook_binds_the_codex_thread_holder_not_a_non_codex_parent(tmp_path, monkeypatch):
    hook, registry = _hook_box(tmp_path, monkeypatch)
    peers = tmp_path / "peers.json"
    out = hook.run({"session_id": THREAD, "cwd": CWD}, peers, registry)
    assert "bound to Codex thread" in out["systemMessage"]
    rows = json.loads(peers.read_text(encoding="utf-8"))
    assert [r["codexPid"] for r in rows if r["target"] == THREAD] == [424226]


def test_the_hook_registers_nothing_for_a_subagent_payload(tmp_path, monkeypatch):
    hook, registry = _hook_box(tmp_path, monkeypatch)
    peers = tmp_path / "peers.json"
    assert hook.run({"session_id": THREAD, "cwd": CWD, "agent_id": "sub-1"}, peers, registry) is None
    assert not peers.exists()
