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


def _proc():
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import importlib
    return importlib.import_module("_unimatrix.proc")


_LIVE_ROW = {"vendor": "codex", "target": THREAD, "codexPid": 4242, "codexProcStart": "s"}


def _holder_live(argv, lock_held, lock_cli):
    return _proc().codex_holder_live(
        _LIVE_ROW, pid_alive_fn=lambda pid: True, proc_start_fn=lambda pid: "s",
        presence_held_fn=lambda target, presence_dir=None: lock_held,
        lock_pids_fn=lambda path: [4242], cli_fn=lambda pid: lock_cli, argv_fn=lambda pid: argv)


def test_a_live_recorded_app_server_holder_does_not_keep_the_face_live() -> None:
    assert _holder_live(["codex", "app-server", "--listen", "unix://"], True, False) is False


def test_a_live_recorded_cli_holder_keeps_the_face_live() -> None:
    assert _holder_live(["codex", "resume"], True, False) is True


def test_a_live_recorded_holder_with_unreadable_argv_keeps_the_face_live() -> None:
    assert _holder_live(None, False, False) is True


def test_a_live_recorded_app_server_holder_defers_to_a_cli_lock_holder() -> None:
    assert _holder_live(["codex", "app-server"], True, True) is True


_PIDLESS_ROW = {"name": NAME, "vendor": "codex", "target": THREAD}


def _pidless_live(lock_held, lock_cli, row=_PIDLESS_ROW):
    return _proc().codex_holder_live(
        row, presence_held_fn=lambda target, presence_dir=None: lock_held,
        lock_pids_fn=lambda path: [4242], cli_fn=lambda pid: lock_cli)


def test_a_pidless_row_with_no_cli_lock_holder_is_not_live() -> None:
    assert _pidless_live(False, False) is False
    assert _pidless_live(True, False) is False


def test_a_pidless_row_whose_lock_a_codex_cli_holds_is_live() -> None:
    assert _pidless_live(True, True) is True


def test_a_pidless_row_with_no_target_or_vendor_stays_unknown() -> None:
    assert _pidless_live(True, True, {"name": NAME, "vendor": "codex"}) is None
    assert _pidless_live(True, True, {"name": NAME, "target": THREAD}) is None


def _pidless_face(started=None):
    face = {"sessionId": f"peer-{NAME}-9", "name": NAME, "vendor": "codex", "target": THREAD}
    if started is not None:
        face["startedAt"] = started
    return face


def test_a_pidless_legacy_face_without_a_cli_lock_holder_is_unbacked_past_grace_or_without_started_at(
        box) -> None:
    (box["tmp"] / "home" / "unimatrix" / "peers.json").write_text(json.dumps([_PIDLESS_ROW]), encoding="utf-8")
    box["classify"](False)
    lv = box["roster"]._liveness()
    old = int((time.time() - 2 * lv.CODEX_FACE_GRACE_SECONDS) * 1000)
    for face in (_pidless_face(), _pidless_face(old)):
        assert lv.codex_face_holder(face) == "lockless"
        assert lv.codex_face_unbacked(face) is True
    assert lv.codex_face_unbacked(_pidless_face(int(time.time() * 1000))) is False


def test_a_pidless_legacy_face_whose_lock_a_codex_cli_holds_stays_live_and_backed(box) -> None:
    (box["tmp"] / "home" / "unimatrix" / "peers.json").write_text(json.dumps([_PIDLESS_ROW]), encoding="utf-8")
    box["classify"](True)
    lv = box["roster"]._liveness()
    face = _pidless_face()
    assert lv.codex_face_holder(face) == "live"
    assert lv.codex_face_unbacked(face) is False
