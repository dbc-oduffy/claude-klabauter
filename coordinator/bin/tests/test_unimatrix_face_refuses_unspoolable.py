"""A pending leg's face acknowledges an inbound frame only when its leg queue took it; otherwise the sender sees a refusal."""
import asyncio
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "face_refuses_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

NAME = "agy-session-5d60623569-on-mac-mini"


def frame(body="please look at this"):
    return {"type": "user", "from": "uds:/far/sender.sock", "message": {"content": body}}


def make_router(tmp_path, adapter, legs):
    r = router.Router([], sessions_dir=tmp_path, state_dir=tmp_path / "state", adapters={"agy": adapter})
    r.pending_legs = legs
    return r


def dispatch(r, peer):
    return asyncio.run(r._dispatch({"op": "inbound", "peer": peer, "frame": frame()}))


def test_inbound_to_a_leg_that_spooled_is_acknowledged(tmp_path):
    async def spool(peer, text):
        return "queued"
    r = make_router(tmp_path, spool, {NAME: {"name": NAME, "target": "5d60623569"}})
    assert dispatch(r, NAME) == {"ok": True}


def test_inbound_the_leg_queue_refuses_is_not_acknowledged(tmp_path):
    async def refuse(peer, text):
        return "[unimatrix-peer-agy] not delivered: no leg queue"
    r = make_router(tmp_path, refuse, {NAME: {"name": NAME, "target": None}})
    answer = dispatch(r, NAME)
    assert answer["ok"] is False and "not delivered" in answer["error"]


def test_inbound_to_a_face_with_no_leg_endpoint_names_the_reason_to_the_sender(tmp_path):
    from _unimatrix.adapters import agy

    async def no_leg(peer, text):
        return agy.NO_LEG
    r = make_router(tmp_path, no_leg, {NAME: {"name": NAME, "target": "5d60623569"}})
    answer = dispatch(r, NAME)
    assert answer["ok"] is False and "no leg endpoint" in answer["error"]


def test_inbound_whose_leg_wrote_a_receipt_is_acknowledged(tmp_path):
    async def written(peer, text):
        return {"written": True, "msg_id": "m1"}
    r = make_router(tmp_path, written, {NAME: {"name": NAME, "target": "5d60623569"}})
    assert dispatch(r, NAME) == {"ok": True}


def test_inbound_whose_adapter_raises_is_not_acknowledged(tmp_path):
    async def boom(peer, text):
        raise OSError("queue dir missing")
    r = make_router(tmp_path, boom, {NAME: {"name": NAME, "target": "x"}})
    answer = dispatch(r, NAME)
    assert answer["ok"] is False and "queue dir missing" in answer["error"]


def _scan(tmp_path, monkeypatch, rec, live):
    import json
    legs = tmp_path / "legs"
    legs.mkdir()
    (legs / "conv-1.json").write_text(json.dumps(rec))
    monkeypatch.setattr(router, "leg_record_live", lambda r: live)
    monkeypatch.setattr(router, "_leg_process_matches", lambda r: True)
    monkeypatch.setattr(router._liveness(), "agy_session_state", lambda c, r: "alive")
    pending = {}
    peers = router.load_leg_peers(legs, pending_out=pending)
    return json.loads((legs / "conv-1.json").read_text()), peers, pending


def test_a_live_leg_record_is_stamped_with_the_name_its_face_answers_to(tmp_path, monkeypatch):
    rec, peers, _ = _scan(tmp_path, monkeypatch, {"conversation": "conv-1", "pid": 1}, True)
    assert rec["name"] == peers[0]["name"] and rec["conversation"] == "conv-1" and rec["pid"] == 1


def test_a_pending_leg_record_is_stamped_too(tmp_path, monkeypatch):
    rec, _, pending = _scan(tmp_path, monkeypatch, {"conversation": "conv-1", "pid": 1, "mode": "pending"}, False)
    assert list(pending) == [rec["name"]]


def test_a_dead_leg_record_is_left_unstamped(tmp_path, monkeypatch):
    rec, peers, pending = _scan(tmp_path, monkeypatch, {"conversation": "conv-1", "pid": 1, "mode": "live"}, False)
    assert "name" not in rec and not peers and not pending


def test_inbound_for_a_gone_pending_leg_is_refused(tmp_path):
    r = make_router(tmp_path, lambda *a: None, {NAME: {"name": NAME, "target": "x"}})
    answer = asyncio.run(r.handle_pending_inbound("other-leg", frame()))
    assert "gone" in answer
