import base64

import pytest

from coordinator_core.group_em import box_key


def _pin_own(home_signer, home_verifier, box_id="box-a"):
    key = box_key.ensure_key(home_signer)
    box_key.pin_peer_key(box_id, base64.b64encode(key).decode(), home_verifier)


def test_ensure_key_mints_once_and_is_stable(tmp_path):
    first = box_key.ensure_key(tmp_path)
    assert len(first) == box_key.KEY_BYTES
    assert box_key.ensure_key(tmp_path) == first
    assert (tmp_path / "state" / "group-em" / "box-key").is_file()


def test_pin_writes_record_and_refuses_invalid_id(tmp_path):
    key_b64 = base64.b64encode(b"k" * 32).decode()
    rec = box_key.pin_peer_key("box-a", key_b64, tmp_path)
    assert rec["box_id"] == "box-a" and rec["key_b64"] == key_b64
    assert (tmp_path / "state" / "group-em" / "peer-keys" / "box-a.json").is_file()
    for bad in ("../x", "", "a/b", "a b"):
        with pytest.raises(ValueError):
            box_key.pin_peer_key(bad, key_b64, tmp_path)
    with pytest.raises(ValueError):
        box_key.pin_peer_key("box-a", "!!not base64!!", tmp_path)


def test_sign_verify_roundtrip_across_homes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _pin_own(a, b)
    rec = {"grantee": "s1", "n": 3, "nested": {"z": 1, "a": [1, 2]}}
    signed = box_key.sign(rec, a)
    assert "sig" not in rec and signed["sig"]
    assert box_key.verify(signed, "box-a", b) is True


def test_sign_replaces_extant_sig(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _pin_own(a, b)
    signed = box_key.sign({"x": 1, "sig": "stale"}, a)
    assert box_key.verify(signed, "box-a", b) is True


def test_missing_sig_is_false(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _pin_own(a, b)
    assert box_key.verify({"x": 1}, "box-a", b) is False
    assert box_key.verify({"x": 1, "sig": ""}, "box-a", b) is False


def test_wrong_key_is_false(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _pin_own(a, b)
    forged = box_key.sign({"x": 1}, c)
    assert box_key.verify(forged, "box-a", b) is False


def test_altered_field_is_false(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _pin_own(a, b)
    signed = box_key.sign({"x": 1, "y": "ok"}, a)
    assert box_key.verify({**signed, "y": "evil"}, "box-a", b) is False
    assert box_key.verify({**signed, "extra": 1}, "box-a", b) is False


def test_unpinned_box_is_false(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    signed = box_key.sign({"x": 1}, a)
    assert box_key.verify(signed, "box-a", b) is False


def test_verify_never_raises(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _pin_own(a, b)
    peer = b / "state" / "group-em" / "peer-keys" / "box-a.json"
    peer.write_text("{not json", encoding="utf-8")
    assert box_key.verify({"x": 1, "sig": "ab"}, "box-a", b) is False
    for rec in (None, "str", 5, [], {"sig": 7}, {"sig": ["x"]}):
        assert box_key.verify(rec, "box-a", b) is False
    assert box_key.verify({"sig": "ab"}, "../evil", b) is False
    assert box_key.verify({"sig": "ab"}, None, b) is False
    assert box_key.verify({"x": object(), "sig": "ab"}, "box-a", b) is False
