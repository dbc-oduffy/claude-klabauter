"""End-to-end pairing across two settings homes: doctor, approve/revoke, relay put, offline verify, autolink.

Box A and box B are two tmp settings homes. The only non-default seam is the signer (a key file instead of
the 1Password agent); `ssh` to box B is answered by running the relay's own main() in-process with
SSH_ORIGINAL_COMMAND set the way sshd would, so the relay, membership library and authorized_keys reconcile
run for real.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required")


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_e2e", BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = load("unimatrix-membership")
R = load("unimatrix-relay")
PAIR = load("unimatrix-pair")
DOCTOR = load("unimatrix-doctor")
AUTOLINK = load("unimatrix-autolink")

CIDR = "192.168.1.0/24"
NO_DESKTOP = lambda *a, **kw: False  # noqa: E731 -- never raise a real notification from a test


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text().strip()


def body(pub):
    return pub.split()[1]


class CountingSigner:
    """The signer fixture injected at the seam: a real key-file signature, counted."""

    def __init__(self, key_file):
        self.calls = 0
        self._inner = M.FakeSigner(key_file, environ={"PATH": os.environ["PATH"]})

    def sign(self, payload):
        self.calls += 1
        return self._inner.sign(payload)


def events(home):
    p = M.membership_dir(home) / "events.log"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.is_file() else []


def kinds(home, kind):
    return [(e.get("box"), e.get("epoch")) for e in events(home) if e["kind"] == kind]


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / "keys").mkdir()
    keys = {n: keygen(tmp_path / "keys" / n) for n in ("signer", "other", "a", "b", "c", "stranger")}
    w = types.SimpleNamespace(keys=keys, tmp=tmp_path, monkeypatch=monkeypatch)
    for box in ("a", "b"):
        home = tmp_path / f"home-{box}"
        (home / "bin").mkdir(parents=True)
        ak = tmp_path / f"user-{box}" / ".ssh" / "authorized_keys"
        ak.parent.mkdir(parents=True)
        setattr(w, f"home_{box}", home)
        setattr(w, f"ak_{box}", ak)
        M.write_pin(home, keys["signer"][1])
    w.signer = CountingSigner(keys["signer"][0])
    # Box B's relay process gets its own membership module instance, desktop notification stubbed.
    w.mem_b = load("unimatrix-membership")
    orig = w.mem_b.notify
    w.mem_b.notify = lambda home, ev, desktop=None: orig(home, ev, desktop=NO_DESKTOP)
    monkeypatch.setattr(R, "load_membership", lambda: w.mem_b)
    w.sshd_calls = []
    return w


def sshd(w):
    """A `run` that answers `ssh <user>@boxb.local membership put|get` by running the relay main() as box B."""
    def run(cmd, **kw):
        if cmd[0] != "ssh":
            return subprocess.run(cmd, **kw)
        w.sshd_calls.append(cmd)
        if not cmd[-3].endswith("@boxb.local"):
            return subprocess.CompletedProcess(cmd, 255, b"", b"ssh: Could not resolve hostname")
        stdin = io.StringIO((kw.get("input") or b"").decode("ascii"))
        out = io.StringIO()
        with w.monkeypatch.context() as mp:
            mp.setenv("COORDINATOR_SETTINGS_HOME", str(w.home_b))
            mp.setenv("SSH_ORIGINAL_COMMAND", " ".join(cmd[-2:]))
            mp.setenv("HOME", str(w.ak_b.parent.parent))
            mp.setenv("USERPROFILE", str(w.ak_b.parent.parent))
            mp.setattr(Path, "home", classmethod(lambda cls: w.ak_b.parent.parent))
            mp.setattr(sys, "stdin", stdin)
            rc = 0
            with contextlib.redirect_stdout(out):
                try:
                    R.main()
                except SystemExit as ex:
                    rc = ex.code if isinstance(ex.code, int) else 1
        return subprocess.CompletedProcess(cmd, rc, out.getvalue().encode(), b"")
    return run


def pair(w, argv, signer=None, environ=None, **kw):
    out, err = io.StringIO(), io.StringIO()
    own = lambda box: (str(w.keys["a"][0]), body(w.keys["a"][1]))  # noqa: E731
    rc = PAIR.main(argv, out=out, err=err, home=w.home_a, environ=environ or {"PATH": os.environ["PATH"]},
                   run=sshd(w), signer=signer, notifier=lambda h, ev: M.notify(h, ev, desktop=NO_DESKTOP),
                   own_key_fn=own, authorized_keys=w.ak_a, is_admin=lambda: False, box="boxa",
                   self_names={"boxa"}, **kw)
    return rc, out.getvalue(), err.getvalue()


def request_file(w, which, box):
    out = io.StringIO()
    PAIR.main(["request", "--box", box, "--cidr", CIDR], out=out,
              own_key_fn=lambda b: (str(w.keys[which][0]), body(w.keys[which][1])))
    f = w.tmp / f"{box}.request.json"
    f.write_text(out.getvalue())
    return str(f)


def test_doctor_names_the_unsigned_line_and_the_dead_alias(world, monkeypatch):
    w = world
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(w.home_a))
    (w.home_a / "bin" / "_machine_local.py").write_text("print('deadbox')\n")
    stranger = f'restrict,command="python {BIN / "unimatrix-relay.py"}" {w.keys["stranger"][1].rsplit(" ", 1)[0]} unimatrix\n'
    w.ak_a.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPERSONAL personal\n" + stranger)

    def run(cmd, **kw):
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(cmd, 0, b"hostname deadbox.local\n", b"")
        return subprocess.run(cmd, **kw)

    def dead(host, port):
        raise OSError("no such host")

    out = io.StringIO()
    rc = DOCTOR.main([], run=run, resolve=dead, authorized_keys=w.ak_a, self_names={"boxa"}, out=out,
                     autolink=AUTOLINK)
    text = out.getvalue()
    assert rc == 1
    assert f"line 2: forced unimatrix-relay.py key ...{body(w.keys['stranger'][1])[-12:]} is in no signed membership record" in text
    assert "line 1" not in text
    assert "deadbox: deadbox.local does not resolve" in text


def test_pairing_end_to_end(world, monkeypatch):
    w = world
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(w.home_a))
    stranger_line = f'restrict,command="python {BIN / "unimatrix-relay.py"}" {w.keys["stranger"][1].rsplit(" ", 1)[0]} unimatrix\n'
    personal = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPERSONAL personal\n"
    w.ak_a.write_text(personal + stranger_line)

    # approve B on a fresh collective: one signature, epoch 1, adopted on A, pushed to B
    rc, out, err = pair(w, ["approve", request_file(w, "b", "boxb"), "--self-cidr", CIDR], signer=w.signer)
    assert rc == 0, err
    assert w.signer.calls == 1
    assert "boxb: pushed" in out
    held_a, _ = M.load_snapshot(w.home_a)
    held_b, _ = M.load_snapshot(w.home_b)
    assert held_a == held_b and held_a["epoch"] == 1
    assert [m["box"] for m in held_a["members"]] == ["boxa", "boxb"]
    assert ("boxb", 1) in kinds(w.home_a, "join") and ("boxb", 1) in kinds(w.home_b, "join")

    # B verifies with no signer: force a real verify against the pin with signing made impossible
    (M.membership_dir(w.home_b) / "verified.json").unlink()
    with monkeypatch.context() as mp:
        mp.setattr(M._KeygenSigner, "sign", lambda *a, **k: pytest.fail("verification constructed a signer"))
        assert {m["box"] for m in M.verified_members(w.home_b)} == {"boxa", "boxb"}

    # B's authorized_keys carries the generated from= pin for A's key
    b_lines = w.ak_b.read_text().splitlines()
    a_line = next(ln for ln in b_lines if body(w.keys["a"][1]) in ln)
    assert a_line.startswith(f'from="{CIDR}",restrict,command=')
    # A's reconcile dropped the unsigned relay line and kept the personal key byte for byte
    a_text = w.ak_a.read_text()
    assert body(w.keys["stranger"][1]) not in a_text and a_text.startswith(personal)

    # approve C then revoke C: each signs once and notifies on A and on B
    before = w.signer.calls
    rc, _, err = pair(w, ["approve", request_file(w, "c", "boxc")], signer=w.signer)
    assert rc == 0, err
    assert w.signer.calls == before + 1
    assert ("boxc", 2) in kinds(w.home_a, "join") and ("boxc", 2) in kinds(w.home_b, "join")
    rc, out, err = pair(w, ["revoke", "boxc"], signer=w.signer)
    assert rc == 0, err
    assert w.signer.calls == before + 2
    assert ("boxc", 3) in kinds(w.home_a, "revoke") and ("boxc", 3) in kinds(w.home_b, "revoke")
    assert M.load_snapshot(w.home_b)[0]["epoch"] == 3
    assert body(w.keys["c"][1]) not in w.ak_b.read_text()

    # a push signed by a key other than the pin is refused on B and surfaced
    forged = M.next_snapshot(M.load_snapshot(w.home_b)[0], {"op": "join", "member": {
        "box": "evil", "host": "evil.local", "user": "me", "relay_pub": w.keys["c"][1].rsplit(" ", 1)[0],
        "from_cidrs": []}})
    sig = M.FakeSigner(w.keys["other"][0], environ={"PATH": os.environ["PATH"]}).sign(M.canonical_bytes(forged))
    import base64
    r = sshd(w)(["ssh", "-i", "k", "me@boxb.local", "membership", "put"],
                input=(M.canonical_bytes(forged).decode() + "\n" + base64.b64encode(sig).decode() + "\n").encode())
    assert r.returncode == 1 and json.loads(r.stdout.decode())["ok"] is False
    assert M.load_snapshot(w.home_b)[0]["epoch"] == 3
    assert [e for e in events(w.home_b) if e["kind"] == "refused"]

    # autolink on A with far_boxes unset and no ssh config links B by the record's host
    spawned = []
    AUTOLINK.run(roster=AUTOLINK._roster(), spawn=lambda cmd: spawned.append(cmd) or types.SimpleNamespace(pid=os.getpid()),
                 probe_fn=lambda *a, **k: None, key_fn=lambda roster, alias: str(w.keys["a"][0]),
                 pull_fn=lambda *a, **k: False, self_names={"boxa"})
    remotes = {cmd[cmd.index("--remote") + 1] for cmd in spawned}
    assert remotes and all(r.endswith("@boxb.local") for r in remotes)
    assert len(spawned) == 2


def test_service_token_refuses_approve_without_signing_or_state(world):
    w = world
    rc, _, err = pair(w, ["approve", request_file(w, "b", "boxb"), "--self-cidr", CIDR], signer=w.signer,
                      environ={"PATH": os.environ["PATH"], "OP_SERVICE_ACCOUNT_TOKEN": "x"})
    assert rc == 1 and "OP_SERVICE_ACCOUNT_TOKEN" in err
    assert w.signer.calls == 0
    assert M.load_snapshot(w.home_a) == (None, None)
