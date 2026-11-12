"""Two distinct far names never resolve to one near session; an unresolvable name is an error, never a delivery."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "name_resolution_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

FAR_A, FAR_B, NEAR = "example-retrieval-repo-ue-addon-1a", "group-em-56", "example-retrieval-repo-48"
MIRROR = {"unimatrixRemote": "machine-a", "macSocketPath": "/far/1.sock", "farPid": 7, "box": "machine-a"}


def write_session(directory, filename, name, endpoint, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(
        {"pid": os.getpid(), "name": name, "messagingSocketPath": endpoint, **extra}))
    key = directory / router._roster().key_name(os.getpid(), endpoint)
    key.write_text(json.dumps({"peerToken": "a" * 32}))


def make_router(tmp_path):
    return router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state")


def test_stale_far_names_on_one_near_session_resolve_to_nothing(tmp_path):
    write_session(tmp_path, "a.json", NEAR, "near-endpoint", unimatrixFarName=FAR_A, **MIRROR)
    r = make_router(tmp_path)
    assert r.resolve_sender(FAR_A, "fallback") == ""
    write_session(tmp_path, "b.json", NEAR, "near-endpoint", unimatrixFarName=FAR_B, **MIRROR)
    assert r.resolve_sender(FAR_B, "fallback") == ""
    assert r.resolve_sender(FAR_A, "fallback") == ""


def test_stale_far_name_send_is_an_error_not_a_delivery(tmp_path):
    write_session(tmp_path, "a.json", NEAR, "near-endpoint", unimatrixFarName=FAR_B, **MIRROR)
    r = make_router(tmp_path)
    response = asyncio.run(r._dispatch({"op": "send", "as": "cli", "to": FAR_B, "text": "hello"}))
    assert not response["ok"] and "no unique live session" in response["error"]
    assert r.faces == {}


def test_unknown_name_fails_closed_despite_a_single_near_candidate(tmp_path):
    write_session(tmp_path, "a.json", NEAR, "near-endpoint")
    r = make_router(tmp_path)
    assert r.resolve_sender(FAR_A, "fallback") == "fallback"
    response = asyncio.run(r._dispatch({"op": "send", "as": "cli", "to": FAR_A, "text": "hello"}))
    assert not response["ok"] and r.faces == {}


def test_labelled_mirrors_and_exact_names_still_resolve_each_to_its_own_session(tmp_path):
    write_session(tmp_path, "a.json", FAR_A + ".machine-a", "ep-a", unimatrixFarName=FAR_A, **MIRROR)
    write_session(tmp_path, "b.json", FAR_B + ".machine-a", "ep-b", unimatrixFarName=FAR_B, **MIRROR)
    r = make_router(tmp_path)
    assert (r.resolve_sender(FAR_A), r.resolve_sender(FAR_B)) == ("ep-a", "ep-b")
    assert r.resolve_sender(FAR_A + ".machine-a") == "ep-a"
    assert r.resolve_sender(NEAR) == ""
