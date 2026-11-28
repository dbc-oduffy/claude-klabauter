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


KEY = {"Machine-a": "AAAAC3NzaC1lZDI1NTE5AAAAIMachineaMachineaMachineaMachineaStrike",
       "Gipsy": "AAAAC3NzaC1lZDI1NTE5AAAAIGipsyGipsyGipsyGipsyGipsyGipsyGipsyG"}


class _HeldMembership:
    class MembershipError(Exception):
        pass

    def __init__(self, boxes):
        self._members = [{"box": b, "host": f"{b}.local", "user": "example-operator", "relay_pub": f"ssh-ed25519 {KEY[b]}"}
                         for b in boxes]
        self.reads = 0

    def verified_members(self, home, run=None):
        self.reads += 1
        return list(self._members)

    @staticmethod
    def member_key_bodies(m):
        return [m["relay_pub"].split()[1]]


def _keys_file(tmp_path, pins):
    """An authorized_keys file with one managed relay line per (pin, key body)."""
    lines = [f'restrict,command="python /x/unimatrix-relay.py --peer {pin}" ssh-ed25519 {body} unimatrix'
             for pin, body in pins]
    path = tmp_path / "authorized_keys"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _with_membership(relay, monkeypatch, tmp_path, boxes, pins=(("stale-pin", KEY["Machine-a"]),)):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    held = _HeldMembership(boxes)
    monkeypatch.setattr(relay, "load_membership", lambda: held)
    monkeypatch.setattr(relay, "effective_authorized_keys", lambda user_file, *a, **k: (_keys_file(tmp_path, pins), False))
    return held


@pytest.fixture(autouse=True)
def _isolated_settings_home(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))


def test_grant_accepts_an_issuer_box_that_differs_from_the_pinned_peer_only_in_case():
    relay, seen = load(), []
    reply = relay.grant(relay.parse_mode(CMD)[1], json.dumps(envelope(issuer_box_id="machine-a")),
                        receive=lambda e: seen.append(e) or {"ok": True}, peer="Machine-a")
    assert reply == {"ok": True} and len(seen) == 1


def test_grant_re_resolves_a_stale_pin_through_the_held_verified_membership(monkeypatch, tmp_path):
    relay, seen = load(), []
    _with_membership(relay, monkeypatch, tmp_path, ["Machine-a", "Gipsy"])
    receive = lambda e: seen.append(e) or {"ok": True}
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope(issuer_box_id="Machine-a")), receive=receive, peer="stale-pin")
    assert reply == {"ok": True} and len(seen) == 1


def test_grant_refuses_an_issuer_the_held_verified_membership_does_not_name(monkeypatch, tmp_path):
    relay = load()
    _with_membership(relay, monkeypatch, tmp_path, ["Machine-a", "Gipsy"])
    args = relay.parse_mode(CMD)[1]
    reply = relay.grant(args, json.dumps(envelope(issuer_box_id="mallory")),
                        receive=lambda e: pytest.fail("engine reached"), peer="stale-pin")
    assert reply == {"ok": False, "reason": "issuer_not_peer"}


def test_a_pin_whose_key_is_another_members_never_signs_for_the_issuer(monkeypatch, tmp_path):
    relay = load()
    _with_membership(relay, monkeypatch, tmp_path, ["Machine-a", "Gipsy"], pins=(("stale-pin", KEY["Gipsy"]),))
    assert relay.peer_accepts("stale-pin", "Machine-a") is False
    assert relay.peer_accepts("stale-pin", "Gipsy") is True


def test_a_pin_with_no_managed_line_is_refused_even_when_the_issuer_is_a_member(monkeypatch, tmp_path):
    relay = load()
    _with_membership(relay, monkeypatch, tmp_path, ["Machine-a"], pins=(("other-pin", KEY["Machine-a"]),))
    assert relay.peer_accepts("stale-pin", "Machine-a") is False


def test_peer_accepts_fails_closed_without_a_usable_held_membership(monkeypatch, tmp_path):
    relay = load()
    held = _with_membership(relay, monkeypatch, tmp_path, ["Machine-a"])
    assert relay.peer_accepts("stale-pin", "Machine-a") is True
    monkeypatch.setattr(relay, "load_membership", lambda: None)
    assert relay.peer_accepts("stale-pin", "Machine-a") is False
    monkeypatch.setattr(relay, "load_membership", lambda: held)
    held._members = []
    assert relay.peer_accepts("stale-pin", "Machine-a") is False
    held._members = [{"box": "Machine-a"}]
    assert relay.peer_accepts("stale-pin", "Machine-a") is False

    def unverified(home, run=None):
        raise held.MembershipError("bad signature")

    held.verified_members = unverified
    assert relay.peer_accepts("stale-pin", "Machine-a") is False


def test_a_fold_equal_pin_never_reads_the_membership_and_a_mismatch_reads_it_once(monkeypatch, tmp_path):
    relay = load()
    held = _with_membership(relay, monkeypatch, tmp_path, ["Machine-a"])
    assert relay.peer_accepts("Machine-a.local", "machine-a") is True
    assert held.reads == 0
    assert relay.peer_accepts("stale-pin", "Machine-a") is True
    assert held.reads == 1


def test_inline_fold_matches_box_registry_fold_box_id():
    from coordinator_core.group_em import box_registry

    relay = load()
    for name in ("Machine-a", "machine-a.local", "Machine-a-LAN", "box-lan.local", ".local", "-lan", "a.local-lan", "x.localx", ""):
        assert relay.fold_box_id(name) == box_registry.fold_box_id(name), name


def test_revoke_re_resolves_a_stale_pin_and_refuses_a_stranger(monkeypatch, tmp_path):
    relay, seen = load(), []
    _with_membership(relay, monkeypatch, tmp_path, ["Machine-a", "Gipsy"])
    receive = lambda env: seen.append(env) or {"ok": True}
    ok = relay.revoke({"grantee": "box-b:sess1"}, json.dumps(revoke_env(issuer_box_id="Machine-a")), receive=receive, peer="stale-pin")
    bad = relay.revoke({"grantee": "box-b:sess1"}, json.dumps(revoke_env(issuer_box_id="mallory")), receive=receive, peer="stale-pin")
    assert ok == {"ok": True} and bad["reason"] == "issuer_not_peer" and len(seen) == 1


def test_keypin_pins_under_the_verified_member_name_not_the_claimed_spelling(monkeypatch, tmp_path):
    relay, seen = load(), []
    _with_membership(relay, monkeypatch, tmp_path, ["Machine-a", "Gipsy"])
    receive = lambda box_id, key, peer: seen.append((box_id, key, peer)) or {"ok": True, "reason": "pinned"}
    line = json.dumps({"box_id": "machine-a", "key_b64": "a2V5"})
    assert relay.keypin({"box_id": "machine-a"}, line, peer="stale-pin", receive=receive)["ok"] is True
    assert seen == [("Machine-a", "a2V5", "Machine-a")]
    assert relay.keypin({"box_id": "gipsy"}, line, peer="stale-pin", receive=receive)["reason"] == "argv_mismatch:box_id"
    stranger = json.dumps({"box_id": "mallory", "key_b64": "a2V5"})
    assert relay.keypin({"box_id": "mallory"}, stranger, peer="stale-pin", receive=receive)["reason"] == "issuer_not_peer"
    assert len(seen) == 1
