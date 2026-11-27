"""A Codex face whose recorded CLI holder is dead lives on a codex CLI/TUI writer-lock holder, never an app-server's."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
THREAD = "01a1fab0-66ce-7073-b564-27f8c32fc2a7"
NAME = "coordinator-content-repo-codex-machine-b-lo-25"
ROOT_ROW = {"cwd": "/w", "source": "cli", "thread_source": "user", "agent_path": None}


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc.wait()
    return proc.pid


def _hold(path: Path):
    """Take the nonblocking process lock a Codex process holds on its thread lock file; returns the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    file = open(path, "r+b")
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return file


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    roster = _load("unimatrix-roster.py", "unimatrix_roster_app_server_lock")
    router = _load("unimatrix-peer-router.py", "unimatrix_router_app_server_lock")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    home = tmp_path / "home"
    (home / "unimatrix").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    peer = {"name": NAME, "vendor": "codex", "target": THREAD, "codexPid": _dead_pid(), "codexProcStart": "gone"}
    (home / "unimatrix" / "peers.json").write_text(json.dumps([peer]), encoding="utf-8")
    lock = _hold(codex_home / "thread-writer-locks" / f"{THREAD}.lock")

    def classify(is_cli: bool) -> None:
        for mod in (roster._liveness(), router._liveness()):
            monkeypatch.setattr(mod, "lock_holder_pids", lambda path: [os.getpid()])
            monkeypatch.setattr(mod, "codex_cli_process", lambda pid: is_cli)

    face = {"pid": os.getpid(), "procStart": roster.proc_start(os.getpid()), "name": NAME, "vendor": "codex",
            "sessionId": f"peer-{NAME}-9", "target": THREAD, "cwd": "work",
            "startedAt": int((time.time() - 5000) * 1000)}
    (sessions / f"{NAME}.json").write_text(json.dumps(face), encoding="utf-8")
    yield {"roster": roster, "router": router, "peer": peer, "face": face, "tmp": tmp_path, "classify": classify}
    lock.close()


def _drive_router(box, monkeypatch):
    router = box["router"]
    tmp = box["tmp"]
    monkeypatch.setattr(router, "codex_thread_metadata", lambda home: {THREAD: ROOT_ROW})
    monkeypatch.setattr(router, "codex_rollout_addressable", lambda *a: True)
    r = router.Router([dict(box["peer"])], sessions_dir=tmp / "sessions", state_dir=tmp / "state",
                      endpoint_dir=tmp / "eps", adapters={})
    stopped = []

    async def stop_face(name):
        stopped.append(name)
        r.faces.pop(name, None)

    monkeypatch.setattr(r, "_stop_face", stop_face)
    r.faces[NAME] = object()
    asyncio.run(r._drop_dead_holders())
    return r, stopped


def _listed(box) -> bool:
    roster = box["roster"]
    face = json.loads((box["tmp"] / "sessions" / f"{NAME}.json").read_text(encoding="utf-8"))
    return bool(roster.listed_row(face, [face]))


def test_an_app_server_holding_the_lock_of_a_dead_holder_face_stops_it_and_unlists_it(box, monkeypatch) -> None:
    box["classify"](False)
    r, stopped = _drive_router(box, monkeypatch)
    assert stopped == [NAME] and NAME in r.dead_holders and NAME not in r.faces
    assert not _listed(box)
    assert box["roster"]._liveness().codex_face_holder(box["face"]) == "dead"


def test_a_codex_cli_holding_the_lock_of_a_dead_holder_face_keeps_it_and_lists_it(box, monkeypatch) -> None:
    box["classify"](True)
    r, stopped = _drive_router(box, monkeypatch)
    assert stopped == [] and NAME not in r.dead_holders and NAME in r.faces
    assert _listed(box)
    assert box["roster"]._liveness().codex_face_holder(box["face"]) == "live"


def test_an_unheld_lock_never_keeps_a_dead_holder_face_live_even_for_a_cli_classification(box, monkeypatch) -> None:
    box["classify"](True)
    for mod in (box["roster"]._liveness(), box["router"]._liveness()):
        monkeypatch.setattr(mod, "agy_presence_held", lambda target, presence_dir=None: False)
    r, stopped = _drive_router(box, monkeypatch)
    assert stopped == [NAME] and not _listed(box)
