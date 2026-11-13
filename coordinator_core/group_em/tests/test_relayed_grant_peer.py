"""relayed_grant_peer: first-contact key pin anchored in verified membership, and signed revoke receipt."""

from __future__ import annotations

import base64
import json

import pytest

from coordinator_core.group_em import box_key, box_registry, member_box, relayed_grant_peer, suite_grant

ISSUER = "box-issuer"


class _Mem:
    class MembershipError(Exception):
        pass

    def __init__(self, members=None, error=None):
        self.members, self.error = members or [], error

    def verified_members(self, home, run=None):
        if self.error:
            raise self.MembershipError(self.error)
        return list(self.members)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    far, near = tmp_path / "far", tmp_path / "near"
    far_id = box_registry.ensure_identity(far)
    key_b64 = base64.b64encode(box_key.ensure_key(near)).decode()
    mem = _Mem([{"box": ISSUER, "host": "h", "user": "u"}])
    monkeypatch.setattr(member_box, "_membership", lambda: mem)
    return {"far": far, "far_id": far_id, "near": near, "key": key_b64, "mem": mem}


def test_pins_a_member_peer_with_no_pin(homes):
    out = relayed_grant_peer.receive_key_pin(ISSUER, homes["key"], ISSUER, homes["far"])
    assert out == {"ok": True, "reason": "pinned"}
    assert box_key.pinned_fingerprint(ISSUER, homes["far"]) == box_key.own_fingerprint(homes["near"])


@pytest.mark.parametrize(
    ("box_id", "peer", "reason"),
    [(ISSUER, None, "no_peer"), (ISSUER, "", "no_peer"), (ISSUER, "box-other", "issuer_not_peer"), ("../x", "../x", "bad_box")],
)
def test_refuses_unanchored_claims_and_writes_no_pin(homes, box_id, peer, reason):
    out = relayed_grant_peer.receive_key_pin(box_id, homes["key"], peer, homes["far"])
    assert (out["ok"], out["reason"]) == (False, reason)
    assert box_key.pinned_fingerprint(ISSUER, homes["far"]) is None


def test_refuses_a_box_absent_from_verified_membership_and_unverifiable_membership(homes):
    homes["mem"].members = [{"box": "box-else", "host": "h", "user": "u"}]
    assert relayed_grant_peer.receive_key_pin(ISSUER, homes["key"], ISSUER, homes["far"])["reason"] == "not_member"
    homes["mem"].members = []
    assert relayed_grant_peer.receive_key_pin(ISSUER, homes["key"], ISSUER, homes["far"])["reason"] == (
        "membership_unverified:no_snapshot"
    )
    homes["mem"].error = "bad signature"
    assert relayed_grant_peer.receive_key_pin(ISSUER, homes["key"], ISSUER, homes["far"])["reason"].startswith(
        "membership_unverified:"
    )
    assert box_key.pinned_fingerprint(ISSUER, homes["far"]) is None


def test_refuses_a_bad_key_and_never_overwrites_an_existing_pin(homes):
    assert relayed_grant_peer.receive_key_pin(ISSUER, "!!", ISSUER, homes["far"])["reason"] == "bad_key"
    box_key.pin_peer_key(ISSUER, base64.b64encode(b"o" * 32).decode(), homes["far"])
    before = box_key.pinned_fingerprint(ISSUER, homes["far"])
    out = relayed_grant_peer.receive_key_pin(ISSUER, homes["key"], ISSUER, homes["far"])
    assert (out["ok"], out["reason"]) == (False, "pin_exists")
    assert box_key.pinned_fingerprint(ISSUER, homes["far"]) == before


def _stored(homes, **over):
    env = {
        "kind": suite_grant.KIND,
        "issuer_box_id": ISSUER,
        "issuer_session_id": "iss",
        "grantee_session_id": "sess1",
        "grantee_box_id": homes["far_id"],
        "issued_at": "2026-11-13T10:00:00Z",
        "expires_at": "2026-11-13T11:00:00Z",
        **over,
    }
    path = suite_grant._relayed_path("sess1", None, homes["far"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"envelope": env, "scope_root": "/r"}), encoding="utf-8")
    return path


def _revoke(homes, **over):
    return box_key.sign(
        {
            "kind": suite_grant.REVOKE_KIND,
            "issuer_box_id": ISSUER,
            "issuer_session_id": "iss",
            "grantee_session_id": "sess1",
            "grantee_box_id": homes["far_id"],
            "revoked_at": "2026-11-13T10:30:00Z",
            **over,
        },
        homes["near"],
    )


def _pin_issuer(homes):
    box_key.pin_peer_key(ISSUER, homes["key"], homes["far"])


def test_revoke_removes_the_grant_and_leaves_a_tombstone(homes):
    path = _stored(homes)
    _pin_issuer(homes)
    out = relayed_grant_peer.receive_relayed_revoke(_revoke(homes), homes["far"])
    assert out == {"ok": True, "reason": "revoked"}
    assert not path.exists()
    tomb = json.loads(path.with_name("sess1.revoked.json").read_text(encoding="utf-8"))
    assert tomb["revoke"]["grantee_session_id"] == "sess1" and tomb["grant"]["issuer_session_id"] == "iss"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"issuer_session_id": "someone-else"}, "issuer_mismatch"),
        ({"revoked_at": "2026-11-13T09:00:00Z"}, "stale_revoke"),
        ({"grantee_box_id": "box-not-me"}, "wrong_grantee_box"),
        ({"kind": "other"}, "bad_record"),
    ],
)
def test_revoke_refusals_leave_the_grant_in_place(homes, over, reason):
    path = _stored(homes)
    _pin_issuer(homes)
    out = relayed_grant_peer.receive_relayed_revoke(_revoke(homes, **over), homes["far"])
    assert (out["ok"], out["reason"]) == (False, reason)
    assert path.exists() and not path.with_name("sess1.revoked.json").exists()


def test_revoke_refuses_unsigned_forged_unpinned_and_absent_grant(homes):
    path = _stored(homes)
    unsigned = {k: v for k, v in _revoke(homes).items() if k != "sig"}
    assert relayed_grant_peer.receive_relayed_revoke(_revoke(homes), homes["far"])["reason"].startswith(
        "bad_signature:no_pin"
    )
    _pin_issuer(homes)
    assert relayed_grant_peer.receive_relayed_revoke(unsigned, homes["far"])["reason"].startswith(
        "bad_signature:unsigned"
    )
    forged = {**_revoke(homes), "revoked_at": "2026-11-13T10:31:00Z"}
    refused = relayed_grant_peer.receive_relayed_revoke(forged, homes["far"])
    assert refused["ok"] is False and refused["reason"].startswith("bad_signature:")
    assert relayed_grant_peer.receive_relayed_revoke("junk", homes["far"])["reason"] == "bad_envelope"
    assert path.exists()
    path.unlink()
    assert relayed_grant_peer.receive_relayed_revoke(_revoke(homes), homes["far"])["reason"] == "no_grant"
