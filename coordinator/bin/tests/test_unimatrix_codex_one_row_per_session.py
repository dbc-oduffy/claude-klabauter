"""One real Codex process is one listed row: three local Codex faces bound to it must list once.

Escape path: `listed_row` judges each Codex face alone. `codex_face_unbacked` answers False for a face whose
holder is live (shared live `codexPid`, shared thread writer lock, or "unknown" inside the grace) and for a
face `codex_face_holder` cannot resolve (None: no `peers.json` row or a non-`peer-` sessionId), so every
face of one Codex passes and the listing carries one row per face.
"""
import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
KLABAUTER_CWD = "C:/dev/claude-klabauter"
MACHINE_A_FACES = ("codex161-codex-machine-a-2289", "codex161-codex-machine-a-2290", "coordinator-content-repo-codex-machine-a-2329")
CODEX_A, CODEX_B = 424201, 424202
THREAD_A, THREAD_B = "019a0000-aaaa-7000-8000-000000000001", "019a0000-bbbb-7000-8000-000000000002"


def _load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Box:
    """A tmp_path Unimatrix box: a sessions dir, a settings home with `peers.json`, a fake process table."""

    def __init__(self, roster, tmp_path, monkeypatch):
        self.roster = roster
        self.sessions = tmp_path / "sessions"
        self.sessions.mkdir()
        self.peers_file = tmp_path / "home" / "unimatrix" / "peers.json"
        self.peers_file.parent.mkdir(parents=True)
        self.live_pids = {os.getpid()}
        self.held_threads = set()
        liveness = roster._liveness()
        monkeypatch.setattr(liveness, "entry_live", lambda d: isinstance(d, dict) and d.get("pid") in self.live_pids)
        monkeypatch.setattr(liveness, "agy_presence_held",
                            lambda conversation, presence_dir=None: conversation in self.held_threads)

    def write(self, faces, peers):
        """Write each face as `<sessions>/<name>.json` and the peers.json rows."""
        for face in faces:
            (self.sessions / f"{face['name']}.json").write_text(json.dumps(face), encoding="utf-8")
        self.peers_file.write_text(json.dumps(peers), encoding="utf-8")

    def listed_codex_names(self):
        """Names of the Codex entries the listing shows: every sessions-dir entry through `listed_row`."""
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.sessions.glob("*.json"))]
        return [r["name"] for r in rows if r.get("vendor") == "codex" and self.roster.listed_row(r, rows)]


@pytest.fixture
def box(tmp_path, monkeypatch):
    roster = _load("unimatrix-roster.py", "unimatrix_roster_one_row")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    return Box(roster, tmp_path, monkeypatch)


def _face(name, target, session_id=None, started_ms=None):
    return {"name": name, "vendor": "codex", "sessionId": session_id or f"peer-{name}", "pid": os.getpid(),
            "cwd": KLABAUTER_CWD, "target": target, "unimatrixRouterPid": os.getpid(),
            "startedAt": int(time.time() * 1000) if started_ms is None else started_ms}


def _peer(name, target, **holder):
    return {"name": name, "vendor": "codex", "target": target, "cwd": KLABAUTER_CWD, **holder}


def _machine_a(box, escape):
    """The three-row Machine-a state for one escape path; ONE live Codex process (CODEX_A) backs all three."""
    box.live_pids.add(CODEX_A)
    names = MACHINE_A_FACES
    if escape == "shared-live-codex-pid":
        faces = [_face(n, f"{THREAD_A[:-1]}{i}") for i, n in enumerate(names)]
        peers = [_peer(n, f["target"], codexPid=CODEX_A) for n, f in zip(names, faces)]
    elif escape == "shared-thread-writer-lock":
        box.held_threads.add(THREAD_A)
        faces = [_face(n, THREAD_A) for n in names]
        peers = [_peer(n, THREAD_A, codexPid=CODEX_A + 100) for n in names]
    elif escape == "unknown-within-grace":
        faces = [_face(n, THREAD_A) for n in names]
        peers = [_peer(n, THREAD_A) for n in names]
    elif escape == "holder-unresolvable-no-peers-row":
        faces = [_face(n, THREAD_A) for n in names]
        peers = []
    elif escape == "holder-unresolvable-non-peer-session-id":
        faces = [_face(n, THREAD_A, session_id=f"codex-{n}") for n in names]
        peers = [_peer(n, THREAD_A, codexPid=CODEX_A) for n in names]
    else:
        raise AssertionError(escape)
    box.write(faces, peers)


ESCAPES = [
    "shared-live-codex-pid",
    "shared-thread-writer-lock",
    "unknown-within-grace",
    "holder-unresolvable-no-peers-row",
    "holder-unresolvable-non-peer-session-id",
]


@pytest.mark.parametrize("escape", ESCAPES)
def test_three_machine_a_faces_for_one_codex_list_one_row(box, escape):
    _machine_a(box, escape)
    listed = box.listed_codex_names()
    assert len(listed) == 1, f"{escape}: {len(listed)} Codex rows listed for one Codex process: {listed}"


def test_two_distinct_live_codex_processes_list_two_rows(box):
    box.live_pids.update({CODEX_A, CODEX_B})
    faces = [_face("codex-machine-a-a", THREAD_A), _face("codex-machine-a-b", THREAD_B)]
    peers = [_peer("codex-machine-a-a", THREAD_A, codexPid=CODEX_A),
             _peer("codex-machine-a-b", THREAD_B, codexPid=CODEX_B)]
    box.write(faces, peers)
    assert sorted(box.listed_codex_names()) == ["codex-machine-a-a", "codex-machine-a-b"]


SURVIVOR = min(MACHINE_A_FACES)


@pytest.mark.parametrize("reregistered", MACHINE_A_FACES)
def test_the_listed_face_keeps_its_name_whichever_face_re_registers_last(box, reregistered):
    """A re-registration resets a face's `startedAt`; the survivor must not move to the newest face."""
    _machine_a(box, "shared-live-codex-pid")
    path = box.sessions / f"{reregistered}.json"
    face = json.loads(path.read_text(encoding="utf-8"))
    face["startedAt"] += 60_000
    path.write_text(json.dumps(face), encoding="utf-8")
    assert box.listed_codex_names() == [SURVIVOR]


def test_the_row_is_listed_before_during_and_after_its_face_re_registers(box, tmp_path, monkeypatch):
    """`Registration.write` rewrites the key then the entry in place; a reader between the two sees one row."""
    router = _load("unimatrix-peer-router.py", "unimatrix_peer_router_one_row")
    _machine_a(box, "shared-live-codex-pid")
    (box.sessions / f"{SURVIVOR}.json").unlink()
    target = f"{THREAD_A[:-1]}0"  # the survivor's thread in `_machine_a`
    reg = router.Registration(SURVIVOR, str(tmp_path / "survivor.sock"), str(box.sessions), cwd=KLABAUTER_CWD,
                              vendor="codex", peer={"name": SURVIVOR, "vendor": "codex", "target": target})
    reg.data["sessionId"] = f"peer-{SURVIVOR}"
    reg.data["startedAt"] -= 60_000
    reg.write()
    assert box.listed_codex_names() == [SURVIVOR]
    seen = []
    write_peer_key = router._roster().write_peer_key

    def paused(*args, **kwargs):
        seen.append(box.listed_codex_names())
        return write_peer_key(*args, **kwargs)

    monkeypatch.setattr(router._roster(), "write_peer_key", paused)
    reg.data["startedAt"] += 120_000
    reg.write()
    assert seen == [[SURVIVOR]]
    assert box.listed_codex_names() == [SURVIVOR]


def _router(router, tmp_path, names, **row):
    """A Router holding `names` as peers of one Codex holder, each with a fake face; faces stop by popping."""
    r = router.Router([], sessions_dir=str(tmp_path / "sessions"), state_dir=str(tmp_path / "state"),
                      codex_locks_dir=str(tmp_path / "codex" / "locks"))
    r.peers = {n: {"name": n, "vendor": "codex", "target": THREAD_A, "codexPid": CODEX_A, **row} for n in names}
    r.faces = {n: object() for n in names}
    stopped = []

    async def stop_face(name):
        r.faces.pop(name)
        stopped.append(name)

    r._stop_face = stop_face
    return r, stopped


def test_the_router_keeps_one_face_for_one_holder_and_keeps_it_through_a_restart(tmp_path):
    router = _load("unimatrix-peer-router.py", "unimatrix_peer_router_one_row_faces")
    r, stopped = _router(router, tmp_path, MACHINE_A_FACES)
    asyncio.run(r._retire_superseded_faces())
    assert sorted(stopped) == sorted(set(MACHINE_A_FACES) - {SURVIVOR})
    assert r.dead_holders == set(stopped) and list(r.faces) == [SURVIVOR]

    r.faces.pop(SURVIVOR)  # the survivor's face is between its stop and its respawn
    asyncio.run(r._retire_superseded_faces())
    assert r.dead_holders == set(stopped), "a survivor mid-restart must not revive a duplicate face"

    r.peers.pop(SURVIVOR)  # the holder is really gone: the next name takes over, still one face
    asyncio.run(r._retire_superseded_faces())
    taker = min(set(MACHINE_A_FACES) - {SURVIVOR})
    assert taker not in r.dead_holders and r.dead_holders == set(MACHINE_A_FACES) - {SURVIVOR, taker}


def test_the_router_never_spawns_a_second_face_for_a_holder_before_the_first_pass(tmp_path):
    router = _load("unimatrix-peer-router.py", "unimatrix_peer_router_one_row_prespawn")
    r, stopped = _router(router, tmp_path, MACHINE_A_FACES)
    r.faces.clear()
    asyncio.run(r._retire_superseded_faces())
    assert r.dead_holders == set(MACHINE_A_FACES) - {SURVIVOR} and stopped == []


def test_the_router_keeps_a_face_per_distinct_holder(tmp_path):
    router = _load("unimatrix-peer-router.py", "unimatrix_peer_router_one_row_distinct")
    r, _ = _router(router, tmp_path, ["codex-machine-a-a", "codex-machine-a-b"])
    r.peers["codex-machine-a-b"].update(target=THREAD_B, codexPid=CODEX_B)
    asyncio.run(r._retire_superseded_faces())
    assert r.dead_holders == set()


@pytest.mark.parametrize("router_pid", [44712, 3620])
def test_a_session_id_face_and_its_peer_transport_face_list_one_row_whatever_the_router_pid(box, router_pid):
    """N1: one Codex name as a UUID-sessionId row plus a `peer-<name>-<pid>` face; the pid is the router's and moves."""
    name = "coordinator-content-repo-codex-machine-a-2329"
    box.live_pids.update({CODEX_A, router_pid})
    uuid_face = _face(name, THREAD_A, session_id="01a204b1-362f-7290-a3cf-09d504a49ecf")
    peer_face = {**_face(name, THREAD_A, session_id=f"peer-{name}-{router_pid}"), "pid": router_pid,
                 "unimatrixRouterPid": router_pid}
    box.peers_file.write_text(json.dumps([_peer(name, THREAD_A, codexPid=CODEX_A)]), encoding="utf-8")
    (box.sessions / "uuid-face.json").write_text(json.dumps(uuid_face), encoding="utf-8")
    (box.sessions / f"peer-face-{router_pid}.json").write_text(json.dumps(peer_face), encoding="utf-8")
    assert box.listed_codex_names() == [name]


def _listed_session_ids(box):
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(box.sessions.glob("*.json"))]
    return [r["sessionId"] for r in rows if r.get("vendor") == "codex" and box.roster.listed_row(r, rows)]


@pytest.mark.parametrize("peer_file_first", [True, False])
@pytest.mark.parametrize("with_target", [True, False])
def test_the_session_id_face_is_the_survivor_and_stays_it_when_the_peer_face_gets_a_new_pid(
        box, peer_file_first, with_target):
    """N1: the merge key has no pid, so the peer face re-registering under pid 3620 -> 44712 never moves the row."""
    name, uuid = "coordinator-content-repo-codex-machine-a-2329", "01a204b1-362f-7290-a3cf-09d504a49ecf"
    box.live_pids.update({CODEX_A, 3620, 44712})
    box.peers_file.write_text(json.dumps([_peer(name, THREAD_A, codexPid=CODEX_A)]), encoding="utf-8")
    uuid_face = _face(name, THREAD_A, session_id=uuid)
    if not with_target:
        del uuid_face["target"]
    uuid_path = box.sessions / ("zz-uuid.json" if peer_file_first else "00-uuid.json")
    uuid_path.write_text(json.dumps(uuid_face), encoding="utf-8")
    for router_pid in (3620, 44712):
        for stale in box.sessions.glob("peer-face-*.json"):
            stale.unlink()
        peer_face = {**_face(name, THREAD_A, session_id=f"peer-{name}-{router_pid}"), "pid": router_pid}
        (box.sessions / f"peer-face-{router_pid}.json").write_text(json.dumps(peer_face), encoding="utf-8")
        assert _listed_session_ids(box) == [uuid]


def test_two_distinct_names_sharing_no_identity_are_not_merged(box):
    """Machine-b: two names dropping together is a shared flap cause, not one Codex."""
    box.live_pids.update({CODEX_A, CODEX_B})
    names = ("experiments-codex-machine-b-lo-27", "general-work-codex-machine-b-lo-122")
    faces = [_face(names[0], THREAD_A), _face(names[1], THREAD_B)]
    box.write(faces, [_peer(names[0], THREAD_A, codexPid=CODEX_A), _peer(names[1], THREAD_B, codexPid=CODEX_B)])
    assert sorted(box.listed_codex_names()) == sorted(names)


def test_a_face_whose_codex_process_is_dead_is_not_listed(box):
    faces = [_face("codex-machine-a-gone", THREAD_A)]
    box.write(faces, [_peer("codex-machine-a-gone", THREAD_A, codexPid=CODEX_B)])
    assert box.listed_codex_names() == []
