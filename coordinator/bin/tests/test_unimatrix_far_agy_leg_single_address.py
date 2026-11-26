"""One Machine-a agy leg held by two far rows resolves to one endpoint; distinct sessions on one name still refuse."""
import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "far_agy_leg_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

LEG = "agy-example-game-workbench-repo-7662a6e205-on-machine-a"
CONV = "7662a6e2-05aa-4c11-9c0e-0123456789ab"
MIRROR = {"unimatrixRemote": "machine-a", "macSocketPath": "/far/leg.sock", "farPid": 7, "box": "machine-a"}


def write_session(directory, filename, name, endpoint, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(
        {"pid": os.getpid(), "name": name, "messagingSocketPath": endpoint, **extra}))
    key = directory / router._roster().key_name(os.getpid(), endpoint)
    key.write_text(json.dumps({"peerToken": "a" * 32}))


def make_router(tmp_path):
    return router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")


def test_bridge_and_relay_mirrors_of_one_agy_leg_resolve_to_one_endpoint(tmp_path):
    write_session(tmp_path, "a.json", LEG, "bridge-ep", vendor="agy", target=CONV, sessionId=f"peer-agy-{CONV}",
                  leg_mode="pending", unimatrixFarName=LEG, **MIRROR)
    write_session(tmp_path, "b.json", LEG, "relay-ep", sessionId=f"peer-agy-{CONV}",
                  unimatrixMirrorName=LEG, unimatrixMirror=True, unimatrixBox="machine-a")
    r = make_router(tmp_path)
    assert r.resolve_sender(LEG) == "bridge-ep", r.last_refusal


def test_leg_face_row_stamped_with_target_only_collapses_with_the_bridge_mirror(tmp_path):
    write_session(tmp_path, "a.json", LEG, "bridge-ep", vendor="agy", target=CONV, sessionId=f"peer-agy-{CONV}",
                  unimatrixFarName=LEG, **MIRROR)
    write_session(tmp_path, "b.json", LEG, "face-ep", vendor="agy", target=CONV,
                  unimatrixMirrorName=LEG, unimatrixMirror=True, unimatrixBox="machine-a")
    r = make_router(tmp_path)
    assert r.resolve_sender(LEG) == "bridge-ep", r.last_refusal


def test_two_distinct_agy_legs_on_one_name_still_refuse(tmp_path):
    write_session(tmp_path, "a.json", LEG, "ep-1", vendor="agy", target=CONV, sessionId=f"peer-agy-{CONV}",
                  unimatrixFarName=LEG, **MIRROR)
    other = "11111111-2222-4333-8444-555555555555"
    write_session(tmp_path, "b.json", LEG, "ep-2", sessionId=f"peer-agy-{other}",
                  unimatrixMirrorName=LEG, unimatrixMirror=True, unimatrixBox="machine-a")
    r = make_router(tmp_path)
    assert r.resolve_sender(LEG) == ""
    assert r.last_refusal.startswith("ambiguous") and "ep-1" in r.last_refusal and "ep-2" in r.last_refusal


def test_same_session_id_under_conflicting_stamped_vendors_still_refuses(tmp_path):
    write_session(tmp_path, "a.json", LEG, "ep-1", vendor="agy", target=CONV, unimatrixFarName=LEG, **MIRROR)
    write_session(tmp_path, "b.json", LEG, "ep-2", vendor="codex", target=CONV, unimatrixMirrorName=LEG,
                  unimatrixMirror=True, unimatrixBox="machine-a")
    r = make_router(tmp_path)
    assert r.resolve_sender(LEG) == ""


def test_unstamped_row_does_not_bridge_agy_and_codex_rows_into_one_owner(tmp_path):
    write_session(tmp_path, "a.json", LEG, "ep-1", vendor="agy", target=CONV, unimatrixFarName=LEG, **MIRROR)
    write_session(tmp_path, "b.json", LEG, "ep-2", vendor="claude", target=CONV, unimatrixMirrorName=LEG,
                  unimatrixMirror=True, unimatrixBox="machine-a")
    write_session(tmp_path, "c.json", LEG, "ep-3", sessionId=f"peer-agy-{CONV}",
                  unimatrixMirrorName=LEG, unimatrixMirror=True, unimatrixBox="machine-a")
    r = make_router(tmp_path)
    assert r.resolve_sender(LEG) == ""
    assert r.last_refusal.startswith("ambiguous")
