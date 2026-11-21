"""Behavior pins for the unimatrix relay `grant` mode."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
RELAY = BIN / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_grant", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def envelope(**over):
    env = {"grantee_session_id": "sess1", "grantee_box_id": "box-b", "note": "suite run",
           "issued_at": "2026-11-05T10:00:00Z", "expires_at": "2026-11-05T11:00:00Z",
           "scope_root": "/repo"}
    env.update(over)
    return env


CMD = "grant box-b:sess1 'suite run' 60 /repo"


def test_parse_accepts_grant_and_maps_fields():
    assert load().parse_mode(CMD) == (
        "grant", {"grantee": "box-b:sess1", "note": "suite run", "ttl": 60, "repo": "/repo"})
    assert load().parse_mode("grant sess1 n 5 -")[1]["repo"] is None


@pytest.mark.parametrize("bad", [
    "grant", "grant a b", "grant a b 60", "grant a b x /r", "grant a b 0 /r",
    "grant a b 60 /r extra", "grant a 'unterminated 60 /r", "granted a b 60 /r"])
def test_parse_refuses_malformed_grant(bad):
    assert load().parse_mode(bad) is None


def test_grant_hands_envelope_to_receive_and_returns_reply():
    relay, seen = load(), []
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope()), receive=lambda e: seen.append(e) or {"ok": True})
    assert reply == {"ok": True} and seen == [envelope()]


@pytest.mark.parametrize("over,reason", [
    ({"grantee_session_id": "other"}, "argv_mismatch:grantee"),
    ({"note": "different"}, "argv_mismatch:note"),
    ({"scope_root": "/elsewhere"}, "argv_mismatch:repo"),
    ({"expires_at": "2026-11-05T12:00:00Z"}, "argv_mismatch:ttl"),
    ({"expires_at": "garbage"}, "bad_ttl"),
])
def test_argv_disagreeing_with_envelope_is_refused_before_the_engine(over, reason):
    relay = load()
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope(**over)), receive=lambda e: pytest.fail("engine reached"))
    assert reply == {"ok": False, "reason": reason}


def test_non_envelope_stdin_is_refused():
    relay = load()
    args = relay.parse_mode(CMD)[1]
    for line in ("", "not json", "[1]"):
        assert relay.grant(args, line, receive=lambda e: pytest.fail("reached")) == {
            "ok": False, "reason": "bad_envelope"}


def test_engine_refusal_passes_through():
    relay = load()
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope()), receive=lambda e: {"ok": False, "reason": "bad_signature"})
    assert reply == {"ok": False, "reason": "bad_signature"}


def revoke_env(**over):
    env = {"kind": "suite-grant-revoke", "issuer_box_id": "box-a", "issuer_session_id": "iss",
           "grantee_session_id": "sess1", "grantee_box_id": "box-b", "revoked_at": "2026-11-05T10:30:00Z"}
    env.update(over)
    return env


def test_parse_accepts_revoke_and_keypin_and_refuses_malformed():
    relay = load()
    assert relay.parse_mode("revoke box-b:sess1") == ("revoke", {"grantee": "box-b:sess1"})
    assert relay.parse_mode("keypin box-a") == ("keypin", {"box_id": "box-a"})
    for bad in ("revoke", "revoke a b", "revoke a;b", "keypin", "keypin a b", "keypin ../x"):
        assert relay.parse_mode(bad) is None


def test_peer_arg_reads_only_a_leading_peer_pair():
    relay = load()
    assert relay.peer_arg(["--peer", "box-a"]) == "box-a"
    assert relay.peer_arg([]) is None and relay.peer_arg(["--peer"]) is None
    assert relay.peer_arg(["--other", "box-a"]) is None and relay.peer_arg(["--peer", "../x"]) is None


def test_grant_revoke_refuse_an_issuer_that_is_not_the_pinned_peer_and_never_reach_receive():
    relay, seen = load(), []
    receive = lambda env: seen.append(env) or {"ok": True}
    grant_args = relay.parse_mode(CMD)[1]
    forged = envelope(issuer_box_id="box-evil")
    assert relay.grant(grant_args, json.dumps(forged), receive=receive, peer="box-a")["reason"] == "issuer_not_peer"
    forged_revoke = revoke_env(issuer_box_id="box-evil")
    out = relay.revoke({"grantee": "box-b:sess1"}, json.dumps(forged_revoke), receive=receive, peer="box-a")
    assert out["reason"] == "issuer_not_peer" and seen == []


def test_grant_and_revoke_from_the_pinned_peer_or_with_no_peer_reach_receive():
    relay, seen = load(), []
    receive = lambda env: seen.append(env) or {"ok": True}
    assert relay.grant(relay.parse_mode(CMD)[1], json.dumps(envelope(issuer_box_id="box-a")), receive=receive, peer="box-a")["ok"]
    assert relay.grant(relay.parse_mode(CMD)[1], json.dumps(envelope()), receive=receive)["ok"]
    assert relay.revoke({"grantee": "box-b:sess1"}, json.dumps(revoke_env()), receive=receive, peer="box-a")["ok"]
    assert relay.revoke({"grantee": "box-b:sess1"}, json.dumps(revoke_env()), receive=receive)["ok"]
    assert len(seen) == 4


def test_revoke_refuses_an_argv_grantee_that_disagrees_and_a_non_envelope():
    relay = load()
    assert relay.revoke({"grantee": "box-b:other"}, json.dumps(revoke_env()), receive=lambda e: {"ok": True})[
        "reason"] == "argv_mismatch:grantee"
    assert relay.revoke({"grantee": "x"}, "not json", receive=lambda e: {"ok": True})["reason"] == "bad_envelope"


def test_keypin_fails_closed_without_a_peer_and_refuses_a_claim_for_another_box():
    relay, seen = load(), []
    receive = lambda box_id, key, peer: seen.append((box_id, key, peer)) or {"ok": True, "reason": "pinned"}
    line = json.dumps({"box_id": "box-a", "key_b64": "a2V5"})
    arg = {"box_id": "box-a"}
    assert relay.keypin(arg, line, peer=None, receive=receive)["reason"] == "no_peer"
    assert relay.keypin(arg, line, peer="box-evil", receive=receive)["reason"] == "issuer_not_peer"
    assert relay.keypin({"box_id": "box-evil"}, line, peer="box-a", receive=receive)["reason"] == "argv_mismatch:box_id"
    assert relay.keypin(arg, "nope", peer="box-a", receive=receive)["reason"] == "bad_envelope"
    assert seen == []
    assert relay.keypin(arg, line, peer="box-a", receive=receive)["ok"] is True
    assert seen == [("box-a", "a2V5", "box-a")]
