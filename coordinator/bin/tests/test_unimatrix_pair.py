"""Tests for unimatrix-pair.py: pin, request, approve, revoke, fetch with a file-key FakeSigner and real ssh-keygen."""
import base64
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]


def _load(name, script):
    spec = importlib.util.spec_from_file_location(name, _BIN / script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


up = _load("unimatrix_pair_under_test", "unimatrix-pair.py")
mem = up._mod("unimatrix-membership.py")


def _keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)], check=True, capture_output=True)
    return Path(str(path) + ".pub").read_text(encoding="utf-8").strip()


def _member(box, tmp_path, cidrs=("10.0.0.0/24",)):
    pub = _keygen(tmp_path / f"relay-{box}")
    return {"box": box, "host": f"{box}.local", "user": "u", "relay_pub": " ".join(pub.split()[:2]), "from_cidrs": list(cidrs)}


class Counting:
    """Wraps a FakeSigner and counts sign() calls."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def sign(self, payload):
        self.calls += 1
        return self.inner.sign(payload)


class Refusing:
    def __init__(self):
        self.calls = 0

    def sign(self, payload):
        self.calls += 1
        raise mem.MembershipError("signer refused")


class Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.skey = tmp_path / "signer"
        self.spub = _keygen(self.skey)
        mem.write_pin(self.home, self.spub)
        self.fake = mem.FakeSigner(self.skey)
        self.ak = tmp_path / "ssh" / "authorized_keys"
        self.ak.parent.mkdir()
        self.ak.write_text("ssh-rsa AAAAunrelated other\n", encoding="utf-8")
        self.events = []
        self.pushes = []
        self.push_reply = {"ok": True, "epoch": 0, "authorized_keys": "updated"}
        self.me = _member("alpha", tmp_path)
        self.beta = _member("beta", tmp_path, ())

    def notifier(self, home, event):
        self.events.append(event)
        return mem.notify(home, event, desktop=lambda *a, **k: True)

    def run(self, cmd, **kw):
        if cmd[0] == "ssh":
            self.pushes.append((cmd, kw.get("input")))
            if self.push_reply is None:
                raise subprocess.TimeoutExpired(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.push_reply).encode(), b"")
        return subprocess.run(cmd, **kw)

    def seed(self, members):
        snap = mem.next_snapshot(None, {"op": "join", "member": members[0]})
        for m in members[1:]:
            snap = mem.next_snapshot(snap, {"op": "join", "member": m})
        snap = {**snap, "epoch": 1, "prev_digest": mem.GENESIS_DIGEST}
        mem.adopt(self.home, snap, self.fake.sign(mem.canonical_bytes(snap)), notifier=lambda *a, **k: None)
        self.events.clear()
        (self.home / "unimatrix" / "membership" / "events.log").unlink(missing_ok=True)
        return snap

    def call(self, argv, signer=None, environ=None, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        rc = up.main(argv, out=out, err=err, stdin=io.StringIO(stdin or ""), home=self.home, environ=environ if environ is not None else {},
                     run=self.run, signer=signer, notifier=self.notifier, own_key_fn=lambda b: (self.tmp / "relay-alpha", self.me["relay_pub"].split()[1]),
                     authorized_keys=self.ak, is_admin=lambda: False, box="alpha", self_names={"alpha"})
        return rc, out.getvalue(), err.getvalue()

    def state(self):
        d = self.home / "unimatrix" / "membership"
        return tuple((d / n).read_bytes() if (d / n).exists() else None for n in ("snapshot.json", "snapshot.sig")) + (self.ak.read_bytes(),)

    def log_lines(self):
        p = self.home / "unimatrix" / "membership" / "events.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_approve_signs_once_writes_next_epoch_logs_and_notifies(rig):
    rig.seed([rig.me, rig.beta])
    gamma = _member("gamma", rig.tmp)
    signer = Counting(rig.fake)
    rc, out, err = rig.call(["approve", "-"], signer=signer, stdin=json.dumps(gamma))
    assert rc == 0, err
    assert signer.calls == 1
    snap, sig = mem.load_snapshot(rig.home)
    assert snap["epoch"] == 2 and {m["box"] for m in snap["members"]} == {"alpha", "beta", "gamma"}
    mem.verify_snapshot(snap, sig, mem.load_pin(rig.home))
    assert len(rig.log_lines()) == 1 and json.loads(rig.log_lines()[0])["kind"] == "join"
    assert [e["kind"] for e in rig.events] == ["join"]
    assert mem.fingerprint(gamma["relay_pub"]) in out and "gamma.local" in out and "10.0.0.0/24" in out
    assert "Ask approval for each new request" in out
    ak = rig.ak.read_text(encoding="utf-8")
    assert "ssh-rsa AAAAunrelated other" in ak and gamma["relay_pub"].split()[1] in ak and 'from="10.0.0.0/24"' in ak


def test_approve_reports_per_member_push_and_skips_self(rig):
    rig.seed([rig.me, rig.beta])
    rc, out, _ = rig.call(["approve", "-"], signer=rig.fake, stdin=json.dumps(_member("gamma", rig.tmp)))
    assert rc == 0
    dialled = sorted(next(a for a in c[0] if "@" in a) for c in rig.pushes)
    assert dialled == ["u@beta.local", "u@gamma.local"]
    assert all(c[0][-2:] == ["membership", "put"] for c in rig.pushes)
    payload = rig.pushes[0][1].decode().splitlines()
    assert json.loads(payload[0])["epoch"] == 2 and base64.b64decode(payload[1])
    assert "beta: pushed" in out and "gamma: pushed" in out


def test_push_to_offline_member_reports_offline(rig):
    rig.seed([rig.me, rig.beta])
    rig.push_reply = None
    rc, out, _ = rig.call(["approve", "-"], signer=rig.fake, stdin=json.dumps(_member("gamma", rig.tmp)))
    assert rc == 0 and "beta: offline" in out and "gamma: offline" in out
    assert mem.load_snapshot(rig.home)[0]["epoch"] == 2


def test_revoke_signs_once_removes_member_and_its_key(rig):
    rig.seed([rig.me, rig.beta])
    rig.call(["approve", "-"], signer=rig.fake, stdin=json.dumps(_member("gamma", rig.tmp)))
    rig.pushes.clear()
    (rig.home / "unimatrix" / "membership" / "events.log").unlink()
    signer = Counting(rig.fake)
    rc, out, err = rig.call(["revoke", "beta"], signer=signer)
    assert rc == 0, err
    assert signer.calls == 1
    snap, _ = mem.load_snapshot(rig.home)
    assert snap["epoch"] == 3 and {m["box"] for m in snap["members"]} == {"alpha", "gamma"}
    assert len(rig.log_lines()) == 1 and json.loads(rig.log_lines()[0])["kind"] == "revoke"
    assert rig.beta["relay_pub"].split()[1] not in rig.ak.read_text(encoding="utf-8")
    assert [next(a for a in c[0] if "@" in a) for c in rig.pushes] == ["u@gamma.local"]
    assert mem.fingerprint(rig.beta["relay_pub"]) in out


def test_refusing_signer_leaves_state_byte_identical(rig):
    rig.seed([rig.me, rig.beta])
    before = rig.state()
    for argv, stdin in ((["approve", "-"], json.dumps(_member("gamma", rig.tmp))), (["revoke", "beta"], None)):
        signer = Refusing()
        rc, _, err = rig.call(argv, signer=signer, stdin=stdin)
        assert rc == 1 and signer.calls == 1 and "signer refused" in err
        assert rig.state() == before
    assert rig.log_lines() == [] and rig.pushes == []


def test_service_token_refused_before_signing(rig):
    rig.seed([rig.me, rig.beta])
    before = rig.state()
    for argv, stdin in ((["approve", "-"], json.dumps(_member("gamma", rig.tmp))), (["revoke", "beta"], None)):
        signer = Counting(rig.fake)
        rc, _, err = rig.call(argv, signer=signer, environ={"OP_SERVICE_ACCOUNT_TOKEN": "x"}, stdin=stdin)
        assert rc == 1 and signer.calls == 0 and "OP_SERVICE_ACCOUNT_TOKEN" in err
        assert rig.state() == before


def test_agent_signer_refuses_service_token_itself(rig):
    signer = up.make_agent_signer(mem, rig.home / "unimatrix" / "membership" / "signer.pub", environ={"OP_SERVICE_ACCOUNT_TOKEN": "x"})
    with pytest.raises(mem.MembershipError, match="OP_SERVICE_ACCOUNT_TOKEN"):
        signer.sign(b"x")


def test_agent_signer_argv_env_and_timeout_message(rig):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        raise subprocess.TimeoutExpired(cmd, 1)

    signer = up.make_agent_signer(mem, "/p/signer.pub", environ={}, run=fake_run, platform="linux")
    with pytest.raises(mem.MembershipError, match="SSH request waiting"):
        signer.sign(b"payload")
    assert seen["cmd"][:5] == ["ssh-keygen", "-Y", "sign", "-U", "-n"] and "/p/signer.pub" in seen["cmd"]
    assert seen["env"]["SSH_AUTH_SOCK"].endswith(".1password/agent.sock")


def test_agent_socket_resolution_per_os():
    assert up.agent_socket({"SSH_AUTH_SOCK": "/already"}, "darwin") == "/already"
    assert up.agent_socket({}, "win32") == up.WINDOWS_AGENT_PIPE
    assert up.agent_socket({}, "darwin", "/h").endswith("2BUA8C4S2C.com.1password/t/agent.sock")
    assert up.agent_socket({}, "linux", "/h").replace("\\", "/") == "/h/.1password/agent.sock"


def test_bad_request_changes_nothing(rig):
    rig.seed([rig.me, rig.beta])
    before = rig.state()
    ip_host = {**_member("gamma", rig.tmp), "host": "10.1.2.3"}
    for text in (json.dumps(ip_host), json.dumps(rig.beta), "not json", json.dumps({"box": "x"})):
        signer = Counting(rig.fake)
        rc, _, _ = rig.call(["approve", "-"], signer=signer, stdin=text)
        assert rc == 1 and signer.calls == 0
        assert rig.state() == before


def test_first_approve_on_fresh_collective_includes_self(rig):
    signer = Counting(rig.fake)
    rc, _, err = rig.call(["approve", "-"], signer=signer, stdin=json.dumps(rig.beta))
    assert rc == 0, err
    snap, _ = mem.load_snapshot(rig.home)
    assert snap["epoch"] == 1 and snap["prev_digest"] == mem.GENESIS_DIGEST
    assert {m["box"] for m in snap["members"]} == {"alpha", "beta"} and signer.calls == 1


def test_pin_writes_prints_fingerprint_and_refuses_repin(tmp_path):
    home = tmp_path / "h"
    pub = _keygen(tmp_path / "k")
    (tmp_path / "k.pub").write_text(pub + "\n", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    assert up.main(["pin", str(tmp_path / "k.pub")], out=out, err=err, home=home) == 0
    fp = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "k.pub")], capture_output=True, text=True).stdout.split()[1]
    assert out.getvalue().strip() == fp
    other = _keygen(tmp_path / "k2")
    (tmp_path / "k2.pub").write_text(other + "\n", encoding="utf-8")
    err2 = io.StringIO()
    assert up.main(["pin", str(tmp_path / "k2.pub")], out=io.StringIO(), err=err2, home=home) == 1
    assert "already exists" in err2.getvalue()
    assert mem.load_pin(home).split()[1] == pub.split()[1]


def test_request_prints_one_join_record(tmp_path):
    out = io.StringIO()
    rc = up.main(["request", "--cidr", "192.168.1.0/24"], out=out, home=tmp_path, own_key_fn=lambda b: (tmp_path / "k", "AAAAbody"), box="alpha")
    assert rc == 0
    rec = json.loads(out.getvalue())
    assert rec["box"] == "alpha" and rec["host"] == "alpha.local" and rec["from_cidrs"] == ["192.168.1.0/24"]
    assert rec["relay_pub"] == "ssh-ed25519 AAAAbody" and set(rec) == set(mem.MEMBER_FIELDS)


def test_fetch_refuses_without_pin(tmp_path):
    err, calls = io.StringIO(), []
    rc = up.main(["fetch", "u@host.local"], out=io.StringIO(), err=err, home=tmp_path, run=lambda *a, **k: calls.append(a))
    assert rc == 1 and "no signer pin" in err.getvalue() and calls == []


def test_fetch_adopts_a_valid_snapshot_and_refuses_a_forged_one(rig):
    snap = mem.next_snapshot(None, {"op": "join", "member": rig.beta})
    sig = rig.fake.sign(mem.canonical_bytes(snap))

    def remote(cmd, **kw):
        assert cmd[-2:] == ["membership", "get"]
        return subprocess.CompletedProcess(cmd, 0, mem.canonical_bytes(snap) + b"\n" + base64.b64encode(sig) + b"\n", b"")

    rig.run = lambda cmd, **kw: remote(cmd, **kw) if cmd[0] == "ssh" else subprocess.run(cmd, **kw)
    rc, out, err = rig.call(["fetch", "u@alpha.local"])
    assert rc == 0, err
    assert mem.load_snapshot(rig.home)[0]["epoch"] == 1
    forger = rig.tmp / "forger"
    _keygen(forger)
    bad_sig = mem.FakeSigner(forger).sign(mem.canonical_bytes(snap))
    (rig.tmp / "second").mkdir()
    rig2 = Rig(rig.tmp / "second")
    rig2.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, mem.canonical_bytes(snap) + b"\n" + base64.b64encode(bad_sig) + b"\n", b"") if cmd[0] == "ssh" else subprocess.run(cmd, **kw)
    rc2, _, err2 = rig2.call(["fetch", "u@alpha.local"])
    assert rc2 == 1 and "refused" in err2
    assert mem.load_snapshot(rig2.home)[0] is None
