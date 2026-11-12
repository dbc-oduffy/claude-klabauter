"""Behavior pins for the unimatrix relay `membership get|put` mode and authorized_keys reconcile."""
import base64
import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required"),
    pytest.mark.complete,
]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_t", BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load("unimatrix-relay")
M = load("unimatrix-membership")
FR = load("unimatrix-file-relay")


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text().strip()


@pytest.fixture
def env(tmp_path):
    k = tmp_path / "keys"
    k.mkdir()
    home = tmp_path / "home"
    ak = tmp_path / "ssh" / "authorized_keys"
    ak.parent.mkdir()
    events = []

    def notifier(h, ev):
        events.append(ev)
        return M.notify(h, ev, desktop=lambda *a, **kw: False)

    return {"keys": {n: keygen(k / n) for n in ("signer", "other", "a", "b", "afile")}, "home": home, "ak": ak,
            "events": events, "notifier": notifier}


def member(env, which, box):
    return {"box": box, "host": f"{box}.local", "user": "me", "relay_pub": env["keys"][which][1],
            "from_cidrs": ["192.168.1.0/24"]}


def sign(env, prev, change, which="signer"):
    snap = M.next_snapshot(prev, change)
    return snap, M.FakeSigner(env["keys"][which][0], environ={}).sign(M.canonical_bytes(snap))


def push(env, snap, sig, **kw):
    return R.membership_put(env["home"], M.canonical_bytes(snap).decode(), base64.b64encode(sig).decode(), M,
                            env["ak"], notifier=env["notifier"], is_admin=lambda: False, **kw)


def pin(env):
    M.write_pin(env["home"], env["keys"]["signer"][1])


def body(env, which):
    return env["keys"][which][1].split()[1]


def _interp(line):
    return line.split('command="')[1].split(" ")[0]


def test_relay_line_on_darwin_names_homebrew_python_before_the_system_one(monkeypatch):
    brew = "/opt/homebrew/bin/python3"
    monkeypatch.setattr(R.sys, "platform", "darwin")
    real_isfile = R.os.path.isfile
    monkeypatch.setattr(R.os.path, "isfile", lambda p: p == brew or real_isfile(p))
    monkeypatch.setattr(R.os, "access", lambda p, m: True)
    assert _interp(R.relay_line("AAAA", "alpha", pin_value=None)) == brew


def test_relay_line_names_the_pin_over_homebrew(monkeypatch):
    monkeypatch.setattr(R.sys, "platform", "darwin")
    monkeypatch.setattr(R.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(R.os, "access", lambda p, m: True)
    assert _interp(R.relay_line("AAAA", "alpha", pin_value="/pinned/python3")) == "/pinned/python3"


def test_relay_line_reads_the_machine_pin_when_none_is_passed(monkeypatch):
    monkeypatch.setattr(R, "_pin_value", lambda: "/machine/python3")
    assert _interp(R.relay_line("AAAA", "alpha")) == "/machine/python3"


def test_relay_line_without_lib_beside_it_uses_the_literal_homebrew_tuple(monkeypatch, tmp_path):
    src = tmp_path / "unimatrix-relay.py"
    src.write_bytes((BIN / "unimatrix-relay.py").read_bytes())
    spec = importlib.util.spec_from_file_location("relay_nolib_t", src)
    solo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(solo)
    monkeypatch.setattr(solo.sys, "platform", "darwin")
    monkeypatch.setattr(solo.os.path, "isfile", lambda p: p == "/usr/local/bin/python3")
    monkeypatch.setattr(solo.os, "access", lambda p, m: True)
    assert _interp(solo.relay_line("AAAA", "alpha", pin_value=None)) == "/usr/local/bin/python3"
    assert _interp(solo.relay_line("AAAA", "alpha", pin_value="/p")) == "/p"


def test_parse_accepts_only_get_and_put():
    assert R.parse_mode("membership get") == ("membership", "get")
    assert R.parse_mode("membership put") == ("membership", "put")
    for bad in ("membership", "membership delete", "membership get x", "membership put extra"):
        assert R.parse_mode(bad) is None


def test_put_adopts_rewrites_only_managed_lines_with_from_pins_and_notifies(env):
    pin(env)
    foreign = "ssh-ed25519 AAAAFOREIGNKEYBODYxxxxxxxx me@laptop\n"
    comment = "# keep me\n"
    stale = R.relay_line(body(env, "a"), "alpha").replace("restrict,", "restrict,port-forwarding,") + "\n"
    env["ak"].write_text(comment + foreign + stale, encoding="utf-8")
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push(env, snap, sig)
    assert reply == {"ok": True, "epoch": 1, "authorized_keys": "updated"}
    text = env["ak"].read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# keep me" and lines[1] == foreign.strip()
    assert "port-forwarding" not in text
    managed = [l for l in lines if R.managed_key_body(l)]
    assert managed == ['from="192.168.1.0/24",' + R.relay_line(body(env, "a"), "alpha")]
    assert managed[0].count(" --peer alpha\"") == 1
    assert [e["kind"] for e in env["events"]] == ["join"]
    assert (env["home"] / "unimatrix" / "membership" / "events.log").read_text().count("join") == 1


def test_put_is_idempotent_and_revoke_removes_the_line(env):
    pin(env)
    s1, g1 = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    s2, g2 = sign(env, s1, {"op": "join", "member": member(env, "b", "beta")})
    push(env, s1, g1)
    assert push(env, s2, g2)["authorized_keys"] == "updated"
    before = env["ak"].read_bytes()
    members = M.verified_members(env["home"])
    assert R.reconcile_authorized_keys(members, env["ak"], M, lambda: False) == "unchanged"
    assert env["ak"].read_bytes() == before
    s3, g3 = sign(env, s2, {"op": "revoke", "box": "alpha"})
    push(env, s3, g3)
    text = env["ak"].read_text()
    assert body(env, "a") not in text and body(env, "b") in text
    assert [e["kind"] for e in env["events"]][-1] == "revoke"


def test_genesis_keeps_an_unlisted_relay_line_and_reports_it_unenrolled(env):
    pin(env)
    hand = R.relay_line(body(env, "other"), "gamma") + "\n"
    env["ak"].write_text(hand, encoding="utf-8")
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push(env, snap, sig)
    assert reply["ok"] is True
    text = env["ak"].read_text(encoding="utf-8")
    assert text.startswith(hand) and body(env, "a") in text
    unenrolled = [e for e in env["events"] if e["kind"] == "unenrolled"]
    assert len(unenrolled) == 1 and body(env, "other")[:24] in unenrolled[0]["detail"]


def test_later_adoption_still_keeps_the_unlisted_line_and_revoking_a_listed_key_drops_only_it(env):
    pin(env)
    hand = R.relay_line(body(env, "other"), "gamma") + "\n"
    env["ak"].write_text(hand, encoding="utf-8")
    s1, g1 = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    s2, g2 = sign(env, s1, {"op": "join", "member": member(env, "b", "beta")})
    s3, g3 = sign(env, s2, {"op": "revoke", "box": "alpha"})
    for s, g in ((s1, g1), (s2, g2), (s3, g3)):
        assert push(env, s, g)["ok"] is True
    text = env["ak"].read_text(encoding="utf-8")
    assert hand in text and body(env, "b") in text and body(env, "a") not in text


def test_stale_pin_of_a_listed_key_is_regenerated(env):
    pin(env)
    s1, g1 = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    push(env, s1, g1)
    good = env["ak"].read_text(encoding="utf-8")
    env["ak"].write_text(good.replace("restrict,", "restrict,port-forwarding,"), encoding="utf-8")
    s2, g2 = sign(env, s1, {"op": "join", "member": member(env, "b", "beta")})
    push(env, s2, g2)
    text = env["ak"].read_text(encoding="utf-8")
    assert "port-forwarding" not in text and good.strip() in text
    assert not [e for e in env["events"] if e["kind"] == "unenrolled"]


def refused(env, reply, snap_state):
    assert reply["ok"] is False
    assert [e["kind"] for e in env["events"]] == ["refused"]
    assert not env["ak"].exists() or env["ak"].read_bytes() == snap_state[0]
    d = env["home"] / "unimatrix" / "membership"
    assert (d / "snapshot.json").exists() is snap_state[1]


def test_put_refuses_a_bad_signature(env):
    pin(env)
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")}, which="other")
    refused(env, push(env, snap, sig), (b"", False))
    assert not env["ak"].exists()


def test_put_refuses_a_replayed_epoch_and_leaves_state_untouched(env):
    pin(env)
    s1, g1 = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    s2, g2 = sign(env, s1, {"op": "join", "member": member(env, "b", "beta")})
    push(env, s2, g2)
    env["events"].clear()
    ak_before = env["ak"].read_bytes()
    snap_before = (env["home"] / "unimatrix" / "membership" / "snapshot.json").read_bytes()
    reply = push(env, s1, g1)
    assert reply["ok"] is False and "replay" in reply["reason"]
    assert [e["kind"] for e in env["events"]] == ["refused"]
    assert env["ak"].read_bytes() == ak_before
    assert (env["home"] / "unimatrix" / "membership" / "snapshot.json").read_bytes() == snap_before


def test_put_refuses_when_no_pin_is_held(env):
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push(env, snap, sig)
    assert reply["ok"] is False and "pin" in reply["reason"]
    assert [e["kind"] for e in env["events"]] == ["refused"]
    assert not env["ak"].exists()
    assert not (env["home"] / "unimatrix" / "membership" / "snapshot.json").exists()


@pytest.mark.parametrize("snap_line,sig_line", [("not json", "AAAA"), ("{}", "!!notb64"), ("[]", ""), ("{}", "")])
def test_put_refuses_malformed_input(env, snap_line, sig_line):
    pin(env)
    reply = R.membership_put(env["home"], snap_line, sig_line, M, env["ak"], notifier=env["notifier"],
                             is_admin=lambda: False)
    assert reply["ok"] is False
    assert [e["kind"] for e in env["events"]] == ["refused"]
    assert not env["ak"].exists()


def test_admin_account_writes_nothing_and_reports_admin(env):
    pin(env)
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = R.membership_put(env["home"], M.canonical_bytes(snap).decode(), base64.b64encode(sig).decode(), M,
                             env["ak"], notifier=env["notifier"], is_admin=lambda: True)
    assert reply["authorized_keys"] == "admin" and not env["ak"].exists()


def test_get_prints_the_held_snapshot_and_none_when_empty(env):
    assert R.membership_get(env["home"], M) is None
    pin(env)
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    push(env, snap, sig)
    line, sig_line = R.membership_get(env["home"], M)
    assert json.loads(line) == snap and base64.b64decode(sig_line) == sig


def test_main_get_and_put_through_ssh_original_command(env, monkeypatch, capsys):
    pin(env)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(env["home"]))
    monkeypatch.setenv("HOME", str(env["ak"].parent.parent))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env["ak"].parent.parent))
    monkeypatch.setattr(R, "load_membership", lambda: M)
    monkeypatch.setattr(M, "_desktop_notify", lambda *a, **kw: False)
    monkeypatch.setattr(M, "notify", lambda home, ev, desktop=lambda *a, **kw: False, _n=M.notify: _n(home, ev, desktop=desktop))
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "membership put")
    monkeypatch.setattr("sys.stdin", io.StringIO(M.canonical_bytes(snap).decode() + "\n" + base64.b64encode(sig).decode() + "\n"))
    with pytest.raises(SystemExit) as ok:
        R.main()
    assert ok.value.code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "membership get")
    R.main()
    assert json.loads(capsys.readouterr().out.splitlines()[0]) == snap
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "membership put")
    monkeypatch.setattr("sys.stdin", io.StringIO("junk\nAAAA\n"))
    with pytest.raises(SystemExit) as ex:
        R.main()
    assert ex.value.code == 1


def test_a_member_with_a_file_key_gets_a_separate_file_leg_line_and_revoke_removes_both(env):
    pin(env)
    m = {**member(env, "a", "alpha"), "file_pub": env["keys"]["afile"][1]}
    s1, g1 = sign(env, None, {"op": "join", "member": m})
    assert push(env, s1, g1)["authorized_keys"] == "updated"
    lines = env["ak"].read_text(encoding="utf-8").splitlines()
    assert lines == ['from="192.168.1.0/24",' + R.relay_line(body(env, "a"), "alpha"),
                     'from="192.168.1.0/24",' + FR.file_line(body(env, "afile"), "alpha")]
    assert "unimatrix-file-relay.py" in lines[1] and "unimatrix-file-relay.py" not in lines[0]
    before = env["ak"].read_bytes()
    assert R.reconcile_authorized_keys(M.verified_members(env["home"]), env["ak"], M, lambda: False) == "unchanged"
    assert env["ak"].read_bytes() == before
    s2, g2 = sign(env, s1, {"op": "revoke", "box": "alpha"})
    push(env, s2, g2)
    assert env["ak"].read_text(encoding="utf-8") == ""


def test_a_member_without_a_file_key_gets_only_the_relay_line_so_messaging_is_unaffected(env):
    pin(env)
    s1, g1 = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    push(env, s1, g1)
    lines = env["ak"].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "unimatrix-file-relay.py" not in lines[0]
    assert R.parse_mode("send 12") == ("send", 12) and R.parse_mode("put set1") is None


def test_dropping_file_pub_from_a_member_revokes_the_file_line_only(env):
    pin(env)
    m = {**member(env, "a", "alpha"), "file_pub": env["keys"]["afile"][1]}
    s1, g1 = sign(env, None, {"op": "join", "member": m})
    push(env, s1, g1)
    s2, g2 = sign(env, s1, {"op": "revoke", "box": "alpha"})
    s3, g3 = sign(env, s2, {"op": "join", "member": member(env, "a", "alpha")})
    push(env, s3, g3)
    text = env["ak"].read_text(encoding="utf-8")
    assert body(env, "a") in text and body(env, "afile") not in text


def push_pin(env, snap, sig, pin_text):
    return R.membership_put(env["home"], M.canonical_bytes(snap).decode(), base64.b64encode(sig).decode(), M,
                            env["ak"], notifier=env["notifier"], is_admin=lambda: False, pin_line=pin_text)


def test_a_pinless_receiver_pins_from_a_verifying_push_and_logs_the_fingerprint(env):
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push_pin(env, snap, sig, env["keys"]["signer"][1] + "\n")
    assert reply["ok"] is True
    assert M.load_pin(env["home"]).split()[1] == body(env, "signer")
    pinned = [e for e in env["events"] if e["kind"] == "pinned"]
    assert len(pinned) == 1 and M.fingerprint(env["keys"]["signer"][1]) in pinned[0]["detail"]
    assert (env["home"] / "unimatrix" / "membership" / "snapshot.json").exists()


def test_a_pin_line_the_snapshot_does_not_verify_against_writes_nothing_and_refuses(env):
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push_pin(env, snap, sig, env["keys"]["other"][1])
    assert reply["ok"] is False
    assert M.load_pin(env["home"]) is None
    assert not (env["home"] / "unimatrix" / "membership" / "signer.pub").exists()
    assert not (env["home"] / "unimatrix" / "membership" / "snapshot.json").exists()
    assert [e["kind"] for e in env["events"]] == ["refused"] and not env["ak"].exists()


def test_a_snapshot_that_fails_to_verify_leaves_no_pin_file_behind(env):
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")}, which="other")
    reply = push_pin(env, snap, sig, env["keys"]["signer"][1])
    assert reply["ok"] is False
    assert not (env["home"] / "unimatrix" / "membership" / "signer.pub").exists()


def test_a_malformed_pin_line_refuses_and_writes_no_pin(env):
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    assert push_pin(env, snap, sig, "not a key")["ok"] is False
    assert M.load_pin(env["home"]) is None


def test_a_receiver_holding_a_different_pin_ignores_the_pushed_pin_and_refuses(env):
    M.write_pin(env["home"], env["keys"]["other"][1])
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push_pin(env, snap, sig, env["keys"]["signer"][1])
    assert reply["ok"] is False
    assert M.load_pin(env["home"]).split()[1] == body(env, "other")
    assert not [e for e in env["events"] if e["kind"] == "pinned"]


def test_a_two_line_push_behaves_as_before_and_an_empty_pin_line_is_a_two_line_push(env):
    pin(env)
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    assert push_pin(env, snap, sig, "")["ok"] is True
    assert [e["kind"] for e in env["events"]] == ["join"]
    env2 = {**env, "home": env["home"].parent / "home2", "events": []}
    snap2, sig2 = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    assert push_pin(env2, snap2, sig2, "")["ok"] is False and M.load_pin(env2["home"]) is None


def test_authorized_keys_gets_one_line_per_relay_key_body_of_each_member(env):
    pin(env)
    m = {**member(env, "a", "alpha"), "relay_pubs": {"beta": env["keys"]["b"][1]}}
    s1, g1 = sign(env, None, {"op": "join", "member": m})
    assert push(env, s1, g1)["authorized_keys"] == "updated"
    lines = env["ak"].read_text(encoding="utf-8").splitlines()
    assert sorted(R.managed_key_body(l) for l in lines) == sorted([body(env, "a"), body(env, "b")])
    s2, g2 = sign(env, s1, {"op": "revoke", "box": "alpha"})
    push(env, s2, g2)
    assert env["ak"].read_text(encoding="utf-8") == ""


def test_main_put_reads_an_optional_third_pin_line(env, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(env["home"]))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: env["ak"].parent.parent))
    monkeypatch.setattr(R, "load_membership", lambda: M)
    monkeypatch.setattr(M, "_desktop_notify", lambda *a, **kw: False)
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "membership put")
    monkeypatch.setattr("sys.stdin", io.StringIO(M.canonical_bytes(snap).decode() + "\n" + base64.b64encode(sig).decode()
                                                 + "\n" + env["keys"]["signer"][1] + "\n"))
    with pytest.raises(SystemExit) as ok:
        R.main()
    assert ok.value.code == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    assert M.load_pin(env["home"]).split()[1] == body(env, "signer")
