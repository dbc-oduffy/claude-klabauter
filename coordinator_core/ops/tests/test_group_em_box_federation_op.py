"""
coordinator_core.ops.tests.test_group_em_box_federation_op -- tests for the four "groupem.box_*"
federation ops (registration, register/place/send round trip on a loopback root, refusals, roster).
"""

from __future__ import annotations

import json

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.group_em import box_registry, box_send, watch
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map
from coordinator_core.ops import group_em_box_federation as op

_OPS = ("box_register", "box_place", "box_send", "box_roster", "suite_grant_receive", "box_key_pin")


@pytest.mark.parametrize("name", _OPS)
def test_registration_quad(name):
    key = f"groupem.{name}"
    assert ipc._REGISTRY.get(key) is not None
    assert _registry_map.OP_MODULE_MAP[key] == "coordinator_core.ops.group_em_box_federation"
    assert OP_KEY_SCOPE[key] == "none"
    want = OpClass.COMPUTE_ONLY if name == "box_roster" else OpClass.MUTATING
    assert OP_CLASSIFICATION[key] is want


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(a))
    return a, b


def test_register_returns_record_and_probes_when_no_capability(homes):
    _, b = homes
    out = op._groupem_box_register({"root": str(b)})
    assert out["registered"] is True
    box = out["box"]
    assert box["root"] == str(b) and box["box_id"] == box_registry.local_box_id(b)
    assert "cpu_count" in box["capability"]
    assert [x["box_id"] for x in box_registry.read_boxes()] == [box["box_id"]]


def test_register_explicit_capability_wins(homes):
    _, b = homes
    out = op._groupem_box_register({"root": str(b), "capability": {"gpu": "5090"}})
    assert out["box"]["capability"] == {"gpu": "5090"}


def test_register_refuses_bad_params(homes):
    assert "error" in op._groupem_box_register({})
    assert "error" in op._groupem_box_register({"root": "x", "capability": "no"})
    assert "error" in op._groupem_box_register(None)


def test_place_both_sides(homes):
    _, b = homes
    box = op._groupem_box_register({"root": str(b), "capability": {"ram_mb": 64000}})["box"]
    ok = op._groupem_box_place({"run": "r1", "requirement": {"min_ram_mb": 32000}})
    assert ok["ok"] is True and ok["placement"]["box_id"] == box["box_id"]
    assert (b / "state" / "group-em" / "box" / "placements" / "r1.json").is_file()
    refused = op._groupem_box_place({"run": "r2", "requirement": {"min_ram_mb": 128000}})
    assert refused == {"ok": False, "reason": "unsatisfied"}
    assert op._groupem_box_place({"run": "r3", "box_id": "nope"})["reason"] == "no_box"


def test_place_refuses_bad_params(homes):
    assert "error" in op._groupem_box_place({})
    assert "error" in op._groupem_box_place({"run": "r", "requirement": []})


def test_send_appends_to_target_inbox_and_refuses_unknown(homes):
    _, b = homes
    box = op._groupem_box_register({"root": str(b), "capability": {}})["box"]
    out = op._groupem_box_send({"box_id": box["box_id"], "to_session": "em1", "text": "hi"})
    assert out["sent"] is True
    (line,) = [json.loads(x) for x in box_send.inbox_path(b).read_text(encoding="utf-8").splitlines()]
    assert line["to_session"] == "em1" and line["text"] == "hi"
    assert op._groupem_box_send({"box_id": "ghost", "to_session": "em1", "text": "x"}) == {
        "sent": False,
        "reason": "unknown_box",
    }
    assert "error" in op._groupem_box_send({"box_id": box["box_id"], "to_session": "em1"})


def test_local_send_stamps_sender_only_when_signed_by_the_pinned_box(homes):
    from coordinator_core.group_em import box_key

    a, b = homes
    box_key.ensure_key(b)
    box_key.pin_peer_key("near", (b / "state" / "group-em" / "box-key").read_text(encoding="ascii"))
    import time

    now = int(time.time())
    claim = box_key.sign(
        {"from_box": "near", "from_session": "sN", "to_session": "em1", "text": "hi", "ts": now}, b
    )
    good = {"local": True, "to_session": "em1", "text": "hi", **{k: claim[k] for k in ("from_box", "from_session", "ts", "sig")}}
    assert op._groupem_box_send(good)["sent"] is True
    forged = {**good, "from_box": "mallory"}
    assert op._groupem_box_send(forged)["sent"] is True
    swapped = {**good, "text": "other"}
    assert op._groupem_box_send(swapped)["sent"] is True
    stale_claim = box_key.sign(
        {"from_box": "near", "from_session": "sN", "to_session": "em1", "text": "hi", "ts": now - 10_000}, b
    )
    stale = {**good, "ts": stale_claim["ts"], "sig": stale_claim["sig"]}
    assert op._groupem_box_send(stale)["sent"] is True
    lines = [json.loads(x) for x in box_send.inbox_path(a).read_text(encoding="utf-8").splitlines()]
    assert (lines[0]["from_box"], lines[0]["from_session"]) == ("near", "sN")
    own = box_registry.local_box_id()
    for bad in lines[1:]:  # forged, swapped, stale
        assert bad["from_box"] == own and "from_session" not in bad


def test_roster_wraps_remote_box_rows(homes, monkeypatch):
    _, b = homes
    box = op._groupem_box_register({"root": str(b), "capability": {}})["box"]
    rows = [{"box_id": box["box_id"], "session_id": "s", "run": "r1", "live": True}]
    monkeypatch.setattr(watch, "remote_box_rows", lambda directory=None: rows, raising=False)
    out = op._groupem_box_roster({})
    assert out == {"boxes": [box["box_id"]], "rows": rows}


def test_roster_never_raises(homes, monkeypatch):
    def boom(directory=None):
        raise RuntimeError("x")

    monkeypatch.setattr(watch, "remote_box_rows", boom, raising=False)
    assert "error" in op._groupem_box_roster({})


def test_box_key_pin_writes_the_peer_key_and_refuses_bad_params(homes):
    import base64

    a, _ = homes
    key_b64 = base64.b64encode(b"k" * 32).decode()
    assert op._groupem_box_key_pin({"box_id": "box-peer", "key_b64": key_b64}) == {
        "pinned": True,
        "box_id": "box-peer",
    }
    assert (a / "state" / "group-em" / "peer-keys" / "box-peer.json").is_file()
    assert "error" in op._groupem_box_key_pin({"box_id": "box-peer"})
    assert "error" in op._groupem_box_key_pin({"box_id": "../x", "key_b64": key_b64})
    assert "error" in op._groupem_box_key_pin({"box_id": "box-peer", "key_b64": "!!notb64"})
    assert "error" in op._groupem_box_key_pin(None)


def test_suite_grant_receive_refuses_a_non_object_and_an_unpinned_issuer(homes):
    assert "error" in op._groupem_suite_grant_receive({})
    assert "error" in op._groupem_suite_grant_receive({"envelope": "x"})
    out = op._groupem_suite_grant_receive({"envelope": {"issuer_box_id": "box-unpinned", "sig": "ab"}})
    assert out == {"ok": False, "reason": "bad_signature"}
