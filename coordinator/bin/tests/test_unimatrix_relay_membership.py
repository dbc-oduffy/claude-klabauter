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

    return {"keys": {n: keygen(k / n) for n in ("signer", "other", "a", "b")}, "home": home, "ak": ak,
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


def test_parse_accepts_only_get_and_put():
    assert R.parse_mode("membership get") == ("membership", "get")
    assert R.parse_mode("membership put") == ("membership", "put")
    for bad in ("membership", "membership delete", "membership get x", "membership put extra"):
        assert R.parse_mode(bad) is None


def test_put_adopts_rewrites_only_managed_lines_with_from_pins_and_notifies(env):
    pin(env)
    foreign = "ssh-ed25519 AAAAFOREIGNKEYBODYxxxxxxxx me@laptop\n"
    comment = "# keep me\n"
    stale = R.relay_line(body(env, "other")) + "\n"
    env["ak"].write_text(comment + foreign + stale, encoding="utf-8")
    snap, sig = sign(env, None, {"op": "join", "member": member(env, "a", "alpha")})
    reply = push(env, snap, sig)
    assert reply == {"ok": True, "epoch": 1, "authorized_keys": "updated"}
    text = env["ak"].read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# keep me" and lines[1] == foreign.strip()
    assert body(env, "other") not in text
    managed = [l for l in lines if R.managed_key_body(l)]
    assert managed == ['from="192.168.1.0/24",' + R.relay_line(body(env, "a"))]
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
