"""Behavior pins for the signed-membership library: real ssh-keygen sign/verify, tmp settings homes."""
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.complete,
    pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required"),
]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load("unimatrix-membership")


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text()


@pytest.fixture
def keys(tmp_path):
    k = tmp_path / "keys"
    k.mkdir()
    return {"signer": keygen(k / "signer"), "other": keygen(k / "other"), "relay": keygen(k / "relay")}


def member(keys, box="alpha", host="alpha.local", cidrs=("192.168.1.0/24",)):
    return {"box": box, "host": host, "user": "me", "relay_pub": keys["relay"][1].strip(), "from_cidrs": list(cidrs)}


def signed(keys, prev, change, which="signer"):
    snap = M.next_snapshot(prev, change)
    return snap, M.FakeSigner(keys[which][0], environ={}).sign(M.canonical_bytes(snap))


def test_verify_accepts_pinned_signature_with_no_signer_constructed(keys, monkeypatch):
    snap, sig = signed(keys, None, {"op": "join", "member": member(keys)})
    monkeypatch.setattr(M, "FakeSigner", None)
    monkeypatch.setattr(M, "AgentSigner", None)
    members = M.verify_snapshot(snap, sig, keys["signer"][1])
    assert [m["box"] for m in members] == ["alpha"]


def test_verify_refuses_wrong_key(keys):
    snap, sig = signed(keys, None, {"op": "join", "member": member(keys)}, which="other")
    with pytest.raises(M.MembershipError, match="does not verify"):
        M.verify_snapshot(snap, sig, keys["signer"][1])


def test_verify_refuses_tampered_snapshot(keys):
    snap, sig = signed(keys, None, {"op": "join", "member": member(keys)})
    snap["members"][0]["host"] = "evil.local"
    with pytest.raises(M.MembershipError, match="does not verify"):
        M.verify_snapshot(snap, sig, keys["signer"][1])


def test_verify_refuses_replayed_epoch_and_accepts_next(keys):
    s1, g1 = signed(keys, None, {"op": "join", "member": member(keys)})
    s2, g2 = signed(keys, s1, {"op": "join", "member": member(keys, "beta", "beta.local")})
    pin = keys["signer"][1]
    with pytest.raises(M.MembershipError, match="replay"):
        M.verify_snapshot(s1, g1, pin, held=s1)
    with pytest.raises(M.MembershipError, match="replay"):
        M.verify_snapshot(s1, g1, pin, held=s2)
    assert len(M.verify_snapshot(s2, g2, pin, held=s1)) == 2


def test_verify_refuses_prev_digest_fork(keys):
    s1, _ = signed(keys, None, {"op": "join", "member": member(keys)})
    fork_base, _ = signed(keys, None, {"op": "join", "member": member(keys, "gamma", "gamma.local")})
    s2, g2 = signed(keys, fork_base, {"op": "join", "member": member(keys, "beta", "beta.local")})
    with pytest.raises(M.MembershipError, match="fork"):
        M.verify_snapshot(s2, g2, keys["signer"][1], held=s1)


def test_verify_accepts_epoch_gap_on_signature_alone(keys):
    s1, _ = signed(keys, None, {"op": "join", "member": member(keys)})
    s2, _ = signed(keys, s1, {"op": "join", "member": member(keys, "beta", "beta.local")})
    s3, g3 = signed(keys, s2, {"op": "join", "member": member(keys, "gamma", "gamma.local")})
    assert len(M.verify_snapshot(s3, g3, keys["signer"][1], held=s1)) == 3


@pytest.mark.parametrize("host", ["192.168.1.5", "::1", "[fe80::1]", "10.0.0.1"])
def test_ip_literal_host_refused_at_build_and_verify(keys, host):
    with pytest.raises(M.MembershipError, match="IP literal"):
        M.next_snapshot(None, {"op": "join", "member": member(keys, host=host)})
    snap, sig = signed(keys, None, {"op": "join", "member": member(keys)})
    snap["members"][0]["host"] = host
    with pytest.raises(M.MembershipError, match="IP literal"):
        M.verify_snapshot(snap, sig, keys["signer"][1])


@pytest.mark.parametrize("cls", ["fake", "agent"])
def test_any_signer_refuses_when_service_token_set(keys, cls):
    calls = []
    run = lambda *a, **k: calls.append(a) or pytest.fail("signer spawned despite service token")
    env = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_x"}
    s = (M.FakeSigner(keys["signer"][0], environ=env, run=run) if cls == "fake"
         else M.AgentSigner(keys["signer"][0], environ=env, run=run))
    with pytest.raises(M.MembershipError, match="OP_SERVICE_ACCOUNT_TOKEN"):
        s.sign(b"x")
    assert not calls


def test_agent_signer_passes_namespace_pin_and_socket(keys):
    seen = {}

    def run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return subprocess.CompletedProcess(cmd, 0, b"SIG", b"")

    out = M.AgentSigner("/p/signer.pub", sock="/s/agent.sock", environ={}, run=run).sign(b"payload")
    assert out == b"SIG"
    assert seen["cmd"][:5] == ["ssh-keygen", "-Y", "sign", "-n", "unimatrix-membership"]
    assert seen["cmd"][-1] == "/p/signer.pub" and seen["env"]["SSH_AUTH_SOCK"] == "/s/agent.sock"


def test_signer_refusal_raises(keys):
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"agent refused")
    with pytest.raises(M.MembershipError, match="refused"):
        M.AgentSigner("/p.pub", environ={}, run=run).sign(b"x")


def test_next_snapshot_chain_and_revoke(keys):
    s1 = M.next_snapshot(None, {"op": "join", "member": member(keys), "collective": "fleet"})
    assert (s1["epoch"], s1["prev_digest"], s1["collective"]) == (1, M.GENESIS_DIGEST, "fleet")
    s2 = M.next_snapshot(s1, {"op": "join", "member": member(keys, "beta", "beta.local")})
    assert (s2["epoch"], s2["prev_digest"]) == (2, M.digest(s1))
    s3 = M.next_snapshot(s2, {"op": "revoke", "box": "alpha"})
    assert [m["box"] for m in s3["members"]] == ["beta"] and s3["epoch"] == 3
    with pytest.raises(M.MembershipError, match="already a member"):
        M.next_snapshot(s2, {"op": "join", "member": member(keys)})
    with pytest.raises(M.MembershipError, match="not a member"):
        M.next_snapshot(s2, {"op": "revoke", "box": "nobody"})


def test_canonical_bytes_independent_of_key_order(keys):
    snap = M.next_snapshot(None, {"op": "join", "member": member(keys)})
    reordered = dict(reversed(list(snap.items())))
    assert M.canonical_bytes(snap) == M.canonical_bytes(reordered)


def test_pin_written_once_and_fingerprint_matches_ssh_keygen(tmp_path, keys):
    pub = keys["signer"][1]
    fp = M.write_pin(tmp_path, pub)
    expect = subprocess.run(["ssh-keygen", "-lf", str(keys["signer"][0]) + ".pub"], capture_output=True, text=True).stdout.split()[1]
    assert fp == expect
    before = M.pin_path(tmp_path).read_text()
    with pytest.raises(M.MembershipError, match="already exists"):
        M.write_pin(tmp_path, keys["other"][1])
    assert M.pin_path(tmp_path).read_text() == before


def test_adopt_writes_logs_and_gates_epoch(tmp_path, keys):
    M.write_pin(tmp_path, keys["signer"][1])
    events = []
    note = lambda home, ev: events.append(ev)
    s1, g1 = signed(keys, None, {"op": "join", "member": member(keys)})
    M.adopt(tmp_path, s1, g1, notifier=note)
    snap, sig = M.load_snapshot(tmp_path)
    assert snap == s1 and sig == g1
    assert events == [{"kind": "join", "box": "alpha", "epoch": 1}]
    with pytest.raises(M.MembershipError, match="replay"):
        M.adopt(tmp_path, s1, g1, notifier=note)
    assert events[-1]["kind"] == "refused"
    s2, g2 = signed(keys, s1, {"op": "revoke", "box": "alpha"})
    M.adopt(tmp_path, s2, g2, notifier=note)
    assert events[-1] == {"kind": "revoke", "box": "alpha", "epoch": 2}
    assert M.load_snapshot(tmp_path)[0]["epoch"] == 2


def test_adopt_refuses_forged_snapshot_without_changing_state(tmp_path, keys):
    M.write_pin(tmp_path, keys["signer"][1])
    s1, g1 = signed(keys, None, {"op": "join", "member": member(keys)})
    M.adopt(tmp_path, s1, g1, notifier=lambda h, e: None)
    before = [(tmp_path / "unimatrix/membership" / n).read_bytes() for n in ("snapshot.json", "snapshot.sig")]
    events = []
    s2, g2 = signed(keys, s1, {"op": "join", "member": member(keys, "evil", "evil.local")}, which="other")
    with pytest.raises(M.MembershipError):
        M.adopt(tmp_path, s2, g2, notifier=lambda h, e: events.append(e))
    after = [(tmp_path / "unimatrix/membership" / n).read_bytes() for n in ("snapshot.json", "snapshot.sig")]
    assert before == after and events[0]["kind"] == "refused"


def test_verified_members_cache_skips_spawn_and_reverifies_on_change(tmp_path, keys):
    M.write_pin(tmp_path, keys["signer"][1])
    s1, g1 = signed(keys, None, {"op": "join", "member": member(keys)})
    M.adopt(tmp_path, s1, g1, notifier=lambda h, e: None)
    boom = lambda *a, **k: pytest.fail("spawned on a cached snapshot")
    assert [m["box"] for m in M.verified_members(tmp_path, run=boom)] == ["alpha"]
    d = tmp_path / "unimatrix/membership"
    (d / "verified.json").unlink()
    assert [m["box"] for m in M.verified_members(tmp_path)] == ["alpha"]
    forged = json.loads((d / "snapshot.json").read_text())
    forged["members"][0]["user"] = "root"
    (d / "snapshot.json").write_bytes(M.canonical_bytes(forged))
    with pytest.raises(M.MembershipError):
        M.verified_members(tmp_path)


def test_verified_members_empty_without_snapshot(tmp_path):
    assert M.verified_members(tmp_path) == []


def test_notify_appends_log_line_and_survives_notifier_failure(tmp_path):
    seen = []
    M.notify(tmp_path, {"kind": "join", "box": "alpha", "epoch": 3}, desktop=lambda t, m: seen.append(m))
    M.notify(tmp_path, {"kind": "revoke", "box": "alpha", "epoch": 4}, desktop=lambda t, m: 1 / 0)
    lines = (tmp_path / "unimatrix/membership/events.log").read_text().splitlines()
    assert [json.loads(ln)["kind"] for ln in lines] == ["join", "revoke"]
    assert seen == ["join alpha 3"]


def test_desktop_notify_without_notifier_is_log_only(monkeypatch):
    monkeypatch.setattr(M.shutil, "which", lambda n: None)
    assert M._desktop_notify("t", "m", run=lambda *a, **k: pytest.fail("spawned")) is False


def test_desktop_notify_on_windows_never_spawns_a_shell(monkeypatch):
    monkeypatch.setattr(M.sys, "platform", "win32")
    monkeypatch.setattr(M.shutil, "which", lambda n: "/x/" + n)
    assert M._desktop_notify("t", "m", run=lambda *a, **k: pytest.fail("spawned")) is False


def test_authorized_keys_lines_prefix_from_pin(keys):
    relay = lambda body: f'restrict,command="python3 relay.py" ssh-ed25519 {body} unimatrix'
    lines = M.authorized_keys_lines([member(keys, cidrs=["10.0.0.0/8", "192.168.1.0/24"]), member(keys, "beta", "beta.local", cidrs=[])], relay)
    body = keys["relay"][1].split()[1]
    assert lines[0] == f'from="10.0.0.0/8,192.168.1.0/24",restrict,command="python3 relay.py" ssh-ed25519 {body} unimatrix'
    assert lines[1].startswith("restrict,command=")
    bad = member(keys)
    bad["from_cidrs"] = ['1.2.3.4",command="x']
    with pytest.raises(M.MembershipError, match="from_cidr"):
        M.authorized_keys_lines([bad], relay)
