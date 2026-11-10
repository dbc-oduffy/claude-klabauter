"""groupem.box_register runs the key exchange: both directions pinned, one-direction onboarding named by id."""

from __future__ import annotations

import pytest

from coordinator_core.group_em import box_key, box_onboarding_pins, box_registry, box_transport
from coordinator_core.ops import group_em_box_federation as op


@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(reg))
    box_transport._reset_limits()
    box_key.ensure_key(reg)
    yield reg
    box_transport._reset_limits()


def _b64_own(home):
    return box_onboarding_pins._b64(box_key._read_own_key(home))


def test_local_new_box_with_one_peer_pins_both_directions(fleet, tmp_path):
    peer = tmp_path / "peer"
    peer.mkdir()
    box_registry.register(peer, {})
    box_key.ensure_key(peer)
    box_key.pin_peer_key(box_registry.local_box_id(peer), _b64_own(peer), home=fleet)
    new = tmp_path / "new"
    new.mkdir()
    box_key.ensure_key(new)

    out = op._groupem_box_register({"root": str(new), "capability": {}})

    new_id, reg_id, peer_id = (box_registry.local_box_id(h) for h in (new, fleet, peer))
    pins = out["pins"]
    assert out["registered"] is True and out["box"]["box_id"] == new_id
    assert sorted(pins["new_box_pinned"]) == sorted([reg_id, peer_id])
    assert pins["new_box_unpinned"] == []
    assert reg_id in pins["peers_pinned_new"]
    assert sorted(pins["peers_pinned_new"]) == sorted([reg_id, peer_id])
    assert pins["peers_unpinned_new"] == []
    assert (new / "state" / "group-em" / "peer-keys" / f"{reg_id}.json").is_file()
    assert (fleet / "state" / "group-em" / "peer-keys" / f"{new_id}.json").is_file()


def test_remote_new_box_without_key_or_route_names_both_directions_unpinned(fleet):
    out = op._groupem_box_register({"root": "/nonexistent/mini", "box_id": "box-mini", "capability": {}})

    assert out["registered"] is True
    pins = out["pins"]
    assert pins["new_box_pinned"] == [] and pins["peers_pinned_new"] == []
    assert [u["id"] for u in pins["new_box_unpinned"]] == [box_registry.local_box_id(fleet)]
    assert pins["new_box_unpinned"][0]["reason"] == "no_route"
    assert pins["peers_unpinned_new"] == [{"id": box_registry.local_box_id(fleet), "reason": "new_key_unavailable"}]


def test_remote_new_box_with_key_b64_pins_the_registrar_only(fleet):
    key = box_onboarding_pins._b64(b"m" * 32)
    out = op._groupem_box_register({"root": "/nonexistent/mini", "box_id": "box-mini", "capability": {}, "key_b64": key})

    reg_id = box_registry.local_box_id(fleet)
    assert out["pins"]["peers_pinned_new"] == [reg_id]
    assert out["pins"]["new_box_unpinned"][0]["id"] == reg_id
    assert box_key.pinned_fingerprint("box-mini", fleet) == box_key.key_fingerprint(b"m" * 32)


def test_pin_failure_never_turns_registered_into_an_error(fleet, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(box_onboarding_pins, "_exchange", boom)
    out = op._groupem_box_register({"root": "/nonexistent/mini", "box_id": "box-mini", "capability": {}})
    assert out["registered"] is True and "error" not in out
    assert out["pins"]["new_box_unpinned"]


def test_register_rejects_bad_box_id_and_transport_types(fleet):
    assert "error" in op._groupem_box_register({"root": "x", "box_id": 3})
    assert "error" in op._groupem_box_register({"root": "x", "transport": "ssh"})


def test_box_key_pin_refuse_mismatch_leaves_a_differing_pin(fleet):
    a, b = box_onboarding_pins._b64(b"a" * 32), box_onboarding_pins._b64(b"b" * 32)
    assert op._groupem_box_key_pin({"box_id": "box-p", "key_b64": a}) == {"pinned": True, "box_id": "box-p"}
    out = op._groupem_box_key_pin({"box_id": "box-p", "key_b64": b, "refuse_mismatch": True})
    assert out == {"pinned": False, "box_id": "box-p", "reason": "pin_mismatch"}
    assert box_key.pinned_fingerprint("box-p", fleet) == box_key.key_fingerprint(b"a" * 32)
    assert op._groupem_box_key_pin({"box_id": "box-p", "key_b64": a, "refuse_mismatch": True})["pinned"] is True
    assert op._groupem_box_key_pin({"box_id": "box-p", "key_b64": b})["pinned"] is True
