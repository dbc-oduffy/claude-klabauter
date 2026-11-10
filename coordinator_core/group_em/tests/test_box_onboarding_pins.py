"""box_onboarding_pins.exchange: both pin directions, named unpinned reasons, one-direction onboarding."""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from coordinator_core.group_em import box_key, box_onboarding_pins, box_registry, box_transport

INVOKE = "/opt/far/bin/coordinator-invoke"


def _ssh_box(box_id, host="far.example"):
    return {
        "box_id": box_id,
        "root": "/nonexistent/far/root",
        "capability": {},
        "transport": {"kind": "ssh", "host": host, "invoke": INVOKE},
    }


def _local_box(box_id_home, capability=None):
    return {"box_id": box_registry.ensure_identity(box_id_home), "root": str(box_id_home), "capability": capability or {}}


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    rows: list = []
    monkeypatch.setattr(box_registry, "lock_dir", lambda home=None: tmp_path / "box-locks")
    monkeypatch.setattr(box_registry, "read_boxes", lambda directory=None: list(rows))
    box_transport._reset_limits()
    registrar = tmp_path / "registrar"
    box_registry._write(box_registry._identity_path(registrar), {"box_id": "box-reg"})
    box_key.ensure_key(registrar)
    yield registrar, rows
    box_transport._reset_limits()


def _b64_own(home):
    return box_onboarding_pins._b64(box_key._read_own_key(home))


def _runner(reply, calls=None, returncode=0):
    def run(argv, timeout):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, json.dumps(reply), "")

    return run


def test_local_new_box_pins_both_directions(fleet, tmp_path):
    registrar, rows = fleet
    peer_home = tmp_path / "peer"
    box_registry._write(box_registry._identity_path(peer_home), {"box_id": "box-peer"})
    box_key.ensure_key(peer_home)
    rows.append({**_local_box(peer_home), "kind": "box"})
    box_key.pin_peer_key("box-peer", _b64_own(peer_home), registrar)
    new_home = tmp_path / "new"
    new_box = _local_box(new_home)
    box_key.ensure_key(new_home)

    out = box_onboarding_pins.exchange(new_box, home=registrar, timeout=5)

    assert sorted(out["new_box_pinned"]) == ["box-peer", "box-reg"] and out["new_box_unpinned"] == []
    assert sorted(out["peers_pinned_new"]) == ["box-peer", "box-reg"] and out["peers_unpinned_new"] == []
    assert box_key._read_peer_key("box-reg", new_home) == box_key._read_own_key(registrar)
    assert box_key._read_peer_key("box-peer", new_home) == box_key._read_own_key(peer_home)
    assert box_key._read_peer_key(new_box["box_id"], registrar) == box_key._read_own_key(new_home)
    assert box_key._read_peer_key(new_box["box_id"], peer_home) == box_key._read_own_key(new_home)


def test_one_direction_onboarding_names_each_unpinned_id(fleet):
    registrar, rows = fleet
    rows.append(_ssh_box("box-mini"))
    rows.append(_ssh_box("box-far", host="other.example"))
    box_key.pin_peer_key("box-far", box_onboarding_pins._b64(b"k" * 32), registrar)
    new_box = _ssh_box("box-new", host="new.example")
    rows.append(new_box)

    def down(argv, timeout):
        return subprocess.CompletedProcess(argv, 255, "", "unreachable")

    out = box_onboarding_pins.exchange(new_box, home=registrar, runner=down, timeout=5)

    assert out["new_box_pinned"] == [] and out["peers_pinned_new"] == []
    unheld = {e["id"]: e["reason"] for e in out["new_box_unpinned"]}
    assert set(unheld) == {"box-reg", "box-mini", "box-far"}
    assert unheld["box-mini"] == "key_not_held" and unheld["box-far"] == "ssh_exit_255"
    reasons = {e["id"]: e["reason"] for e in out["peers_unpinned_new"]}
    assert reasons == {"box-reg": "new_key_unavailable", "box-mini": "new_key_unavailable", "box-far": "new_key_unavailable"}


def test_key_b64_pins_registrar_and_pushes_remote_with_refuse_mismatch(fleet):
    registrar, rows = fleet
    rows.append(_ssh_box("box-peer"))
    new_box = _ssh_box("box-new", host="new.example")
    rows.append(new_box)
    new_key = box_onboarding_pins._b64(b"n" * 32)
    calls: list = []

    out = box_onboarding_pins.exchange(
        new_box, home=registrar, key_b64=new_key, runner=_runner({"pinned": True}, calls), timeout=5
    )

    assert out["peers_pinned_new"] == ["box-reg", "box-peer"] and out["peers_unpinned_new"] == []
    assert box_key._read_peer_key("box-new", registrar) == b"n" * 32
    assert "box-reg" in out["new_box_pinned"]
    sent = [json.loads(shlex.split(c[-1])[2]) for c in calls]
    assert all(p["refuse_mismatch"] is True and p["box_id"] in ("box-reg", "box-new") for p in sent)
    assert {"box_id": "box-new", "key_b64": new_key, "refuse_mismatch": True} in sent


def test_remote_not_pinned_reply_maps_to_unpinned_with_reason(fleet):
    registrar, rows = fleet
    rows.append(_ssh_box("box-peer"))
    new_box = _ssh_box("box-new", host="new.example")
    rows.append(new_box)
    out = box_onboarding_pins.exchange(
        new_box,
        home=registrar,
        key_b64=box_onboarding_pins._b64(b"n" * 32),
        runner=_runner({"pinned": False, "reason": "pin_mismatch"}),
        timeout=5,
    )
    assert out["peers_unpinned_new"] == [{"id": "box-peer", "reason": "pin_mismatch"}]
    assert out["peers_pinned_new"] == ["box-reg"]
    assert out["new_box_unpinned"] == [
        {"id": "box-reg", "reason": "pin_mismatch"},
        {"id": "box-peer", "reason": "key_not_held"},
    ]


def test_differing_extant_local_pin_is_reported_and_left_alone(fleet, tmp_path):
    registrar, rows = fleet
    new_home = tmp_path / "new"
    new_box = _local_box(new_home)
    box_key.ensure_key(new_home)
    box_key.pin_peer_key(new_box["box_id"], box_onboarding_pins._b64(b"x" * 32), registrar)

    out = box_onboarding_pins.exchange(new_box, home=registrar, timeout=5)

    assert out["peers_unpinned_new"] == [{"id": "box-reg", "reason": "pin_mismatch"}]
    assert box_key._read_peer_key(new_box["box_id"], registrar) == b"x" * 32


def test_no_route_peer_is_named(fleet):
    registrar, rows = fleet
    rows.append({"box_id": "box-lost", "root": "/nonexistent/lost", "capability": {}})
    out = box_onboarding_pins.exchange(
        _ssh_box("box-new"), home=registrar, key_b64=box_onboarding_pins._b64(b"n" * 32),
        runner=_runner({"pinned": True}), timeout=5,
    )
    assert {"id": "box-lost", "reason": "no_route"} in out["peers_unpinned_new"]


def test_registering_self_skips_the_registrar(fleet):
    registrar, rows = fleet
    out = box_onboarding_pins.exchange(
        {"box_id": "box-reg", "root": str(registrar), "capability": {}}, home=registrar, timeout=5
    )
    assert out == {"new_box_pinned": [], "new_box_unpinned": [], "peers_pinned_new": [], "peers_unpinned_new": []}


def test_never_raises_on_garbage(fleet):
    registrar, _ = fleet
    for bad in (None, {}, {"box_id": "../x"}, "str"):
        out = box_onboarding_pins.exchange(bad, home=registrar, timeout=5)
        assert set(out) == {"new_box_pinned", "new_box_unpinned", "peers_pinned_new", "peers_unpinned_new"}
        assert out["new_box_unpinned"]
