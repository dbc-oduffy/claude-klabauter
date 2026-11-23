"""A relay-pushed mirror answers to its bare name; one session behind two mirror routes is one address."""
import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pushed_mirror_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

NAME, SID, OTHER_SID = "claude-klabauter-0e", "217c73d7-2e34-4008-bb67-298d4f9c067a", "0a1b2c3d-1111-2222-3333-444455556666"
# Mac-mini incident 2026-11-23: the roster's bridge mirror was unmirrored; only this pushed row remained.
PUSHED = {"name": f"{NAME}.machine-a", "unimatrixMirrorName": NAME, "unimatrixBox": "machine-a", "unimatrixMirror": True,
          "sessionId": SID}
BRIDGE = {"name": f"{NAME}.machine-a", "unimatrixFarName": NAME, "unimatrixRemote": "machine-a",
          "macSocketPath": "/far/1.sock", "sessionId": SID}


def write_session(directory, filename, endpoint, row):
    (directory / filename).write_text(json.dumps({"pid": os.getpid(), "messagingSocketPath": endpoint, **row}))
    (directory / router._roster().key_name(os.getpid(), endpoint)).write_text(json.dumps({"peerToken": "a" * 32}))


def make_router(tmp_path):
    return router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")


def test_a_pushed_mirror_alone_resolves_by_its_bare_name(tmp_path):
    write_session(tmp_path, "p.json", "ep-pushed", PUSHED)
    r = make_router(tmp_path)
    assert r.resolve_sender(NAME) == "ep-pushed"
    assert r.resolve_sender(f"{NAME}.machine-a") == "ep-pushed"


def test_one_session_behind_bridge_and_pushed_mirrors_resolves_to_the_bridge(tmp_path):
    write_session(tmp_path, "p.json", "ep-pushed", PUSHED)
    write_session(tmp_path, "b.json", "ep-bridge", BRIDGE)
    r = make_router(tmp_path)
    assert r.resolve_sender(NAME) == "ep-bridge"
    assert r.resolve_sender(f"{NAME}.machine-a") == "ep-bridge"


def test_two_sessions_on_one_name_stay_ambiguous(tmp_path):
    write_session(tmp_path, "p.json", "ep-pushed", PUSHED)
    write_session(tmp_path, "b.json", "ep-bridge", {**BRIDGE, "sessionId": OTHER_SID})
    r = make_router(tmp_path)
    assert r.resolve_sender(NAME, "fallback") == ""
    assert "ambiguous" in r.last_refusal


def test_a_non_mirror_row_does_not_gain_its_mirror_name_field(tmp_path):
    write_session(tmp_path, "p.json", "ep-pushed", {**PUSHED, "unimatrixMirror": False})
    r = make_router(tmp_path)
    assert r.resolve_sender(NAME, "fallback") == "fallback"
