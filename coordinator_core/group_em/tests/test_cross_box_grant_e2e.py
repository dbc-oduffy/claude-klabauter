"""Cross-box ``group-em-grant`` end to end: two settings homes, a signed membership roster, the default
``tier-u-grant-cli`` argv (``group-em-grant`` / ``check`` / ``group-em-revoke``).

Box A (issuer, live verified holder) has no ``box.json`` row for box B: B is reached only through the
verified membership snapshot both homes hold. The injected runner executes B's relay modes in-process
under the forced-command peer A, so no ssh spawns; membership verification is a cache hit.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.group_em import (
    box_key,
    box_registry,
    box_transport,
    member_box,
    node_transport,
    nomination,
    relayed_grant_peer,
    session_registry,
    suite_grant,
)
from coordinator_core.session import grant as session_grant

pytestmark = [
    pytest.mark.complete,
    pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required"),
]

HOLDER, PEER = "sid-holder", "sid-peer"
BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
RELAY_KEY = "/keys/unimatrix"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cli():
    mod = _load("tier_u_grant_cli_e2e", "tier-u-grant-cli.py")
    mod._import_module = lambda: session_grant
    return mod


def _keygen(path: Path) -> tuple[Path, str]:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text().strip()


@pytest.fixture(scope="module")
def signed_membership(tmp_path_factory):
    """A template home holding a membership snapshot signed by a test ssh key and adopted (verified.json
    seeded), listing boxes ``box-aaaa0001`` and ``box-bbbb0002``; each test copies it into its homes."""
    root = tmp_path_factory.mktemp("membership-template")
    keys = {name: _keygen(root / name) for name in ("signer", "relay-a", "relay-b")}
    mem = member_box._membership()
    snap = None
    for box, pub in (("box-aaaa0001", keys["relay-a"][1]), ("box-bbbb0002", keys["relay-b"][1])):
        member = {"box": box, "host": f"{box}.local", "user": "tester", "relay_pub": pub, "from_cidrs": ["10.0.0.0/24"]}
        snap = mem.next_snapshot(snap, {"op": "join", "member": member})
    sig = mem.FakeSigner(keys["signer"][0], environ={}).sign(mem.canonical_bytes(snap))
    home = root / "home"
    mem.write_pin(home, keys["signer"][1])
    # adopt's ssh verify does `import lib` then `from cc_invoke import ...`: an ambient namespace `lib`
    # on sys.path would shadow coordinator/bin/lib, so bind the bin package for the adopt only.
    with pytest.MonkeyPatch.context() as mp:
        mp.syspath_prepend(str(BIN))
        mp.delitem(sys.modules, "lib", raising=False)
        mem.adopt(home, snap, sig, notifier=lambda *_: None)
    return mem.membership_dir(home)


class _Autolink:
    """Relay-key lookup stand-in: the real one reads the operator's machine-local key registry."""

    def machine_local_key(self, roster, alias, run=None):
        return None

    def ssh_identity_file(self, roster, alias, run=None):
        return None

    def resolve_key(self, roster, alias, local_fn=None, ssh_fn=None):
        return RELAY_KEY


@pytest.fixture
def world(tmp_path, monkeypatch, signed_membership):
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    repo = tmp_path / "repo"
    repo.mkdir()
    ids = {"a": "box-aaaa0001", "b": "box-bbbb0002"}
    for which, home in (("a", home_a), ("b", home_b)):
        box_registry._write(
            box_registry._identity_path(home), {"schema_version": 1, "box_id": ids[which], "minted_at": 0}
        )
        shutil.copytree(signed_membership, member_box._membership().membership_dir(home))
    dir_a = home_a / "state" / "group-em"
    live = {HOLDER, PEER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(suite_grant, "session_live", lambda sid, cwd=None: sid in live)
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [
            session_registry.RegistryRow(session_id=PEER, name="peer", pid=1, cwd=str(repo), status="")
        ],
    )
    monkeypatch.setattr(member_box, "_autolink", lambda: _Autolink())
    monkeypatch.setattr(member_box, "_roster", lambda: object())
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home_a))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    dir_a.mkdir(parents=True, exist_ok=True)
    nomination.claim(str(repo), HOLDER, directory=dir_a, prompt_id="p-1")
    box_transport._reset_limits()

    relay = _load("unimatrix_relay_e2e", "unimatrix-relay.py")
    calls: list = []

    def runner(argv, timeout, stdin_text):
        """B's forced command: parse the mode and run it in-process under the pinned peer A."""
        calls.append(argv[-1])
        mode, arg = relay.parse_mode(argv[-1])
        if mode == "grant":
            reply = relay.grant(
                arg, stdin_text, peer=ids["a"],
                receive=lambda env: suite_grant.receive_relayed_grant(env, home=home_b),
            )
        elif mode == "revoke":
            reply = relay.revoke(
                arg, stdin_text, peer=ids["a"],
                receive=lambda env: relayed_grant_peer.receive_relayed_revoke(env, home_b),
            )
        else:
            reply = relay.keypin(
                arg, stdin_text, peer=ids["a"],
                receive=lambda box, key, peer: relayed_grant_peer.receive_key_pin(box, key, peer, home_b),
            )
        return subprocess.CompletedProcess(argv, 0 if reply.get("ok") is True else 1, json.dumps(reply) + "\n", "")

    monkeypatch.setattr(
        node_transport.SshTransport, "__init__",
        lambda self, r=None, directory=None: setattr(self, "runner", runner) or setattr(self, "directory", directory),
    )
    return {
        "mp": monkeypatch, "home_a": home_a, "home_b": home_b, "ids": ids, "dir_a": dir_a,
        "calls": calls, "runner": runner, "cli": _cli(),
        "target": f"{ids['b']}:{PEER}",
    }


def _as(w, which, session):
    w["mp"].setenv("COORDINATOR_SETTINGS_HOME", str(w[f"home_{which}"]))
    w["mp"].setenv("COORDINATOR_SESSION_ID", session)


def _no_spawn(w):
    def refuse(*a, **k):
        raise AssertionError("subprocess spawned on the grant/check/revoke path")

    w["mp"].setattr(subprocess, "run", refuse)
    w["mp"].setattr(subprocess, "Popen", refuse)


def _relayed_file(w) -> Path:
    return w["home_b"] / "state" / "group-em" / "suite-grants" / "relayed" / f"{PEER}.json"


def _grant(w, capsys, target=None):
    _as(w, "a", HOLDER)
    code = w["cli"].main(["group-em-grant", target or w["target"], "ruling"])
    out = capsys.readouterr()
    return code, out.out + out.err


def _check(w, capsys):
    _as(w, "b", PEER)
    code = w["cli"].main(["check"])
    out = capsys.readouterr()
    return code, out.out + out.err


def _edit_relayed(w, edit):
    path = _relayed_file(w)
    body = json.loads(path.read_text(encoding="utf-8"))
    edit(body["envelope"])
    path.write_text(json.dumps(body), encoding="utf-8")


def test_member_box_absent_from_box_json_is_granted_and_b_checks_granted(world, capsys):
    w = world
    assert box_registry.read_boxes(w["dir_a"]) == []
    _no_spawn(w)
    code, out = _grant(w, capsys)
    assert code == 0, out
    assert w["calls"][0].startswith("grant ") and "keypin" in w["calls"][1]
    assert _relayed_file(w).is_file()
    stored = json.loads(_relayed_file(w).read_text(encoding="utf-8"))["envelope"]
    assert (stored["issuer_box_id"], stored["issuer_session_id"]) == (w["ids"]["a"], HOLDER)
    assert (stored["grantee_box_id"], stored["grantee_session_id"]) == (w["ids"]["b"], PEER)
    assert nomination.box_holder(w["home_b"] / "state" / "group-em") is None, "B has no live Group EM"
    assert nomination.box_holder(w["dir_a"]) is not None
    assert box_key.pinned_fingerprint(w["ids"]["a"], w["home_b"]) == box_key.own_fingerprint(w["home_a"])
    code, out = _check(w, capsys)
    assert code == 0 and "basis: group-em-grant" in out


def test_non_member_box_is_box_unknown_and_nothing_is_delivered(world, capsys):
    w = world
    _no_spawn(w)
    code, out = _grant(w, capsys, target=f"box-ghost0003:{PEER}")
    assert code != 0 and "BOX_UNKNOWN" in out
    assert w["calls"] == [] and not _relayed_file(w).exists()
    assert box_registry.read_boxes(w["dir_a"]) == []


def test_expired_grant_reads_expired_on_b(world, capsys):
    w = world
    assert _grant(w, capsys)[0] == 0
    real = suite_grant.time.time
    w["mp"].setattr(suite_grant.time, "time", lambda: real() + (suite_grant.TTL_DEFAULT_MINUTES * 60) + 600)
    code, out = _check(w, capsys)
    assert code != 0 and "expired" in out and "basis:" not in out


def test_revoke_through_the_default_argv_reaches_b_and_check_reads_revoked(world, capsys):
    w = world
    assert _grant(w, capsys)[0] == 0
    _as(w, "a", HOLDER)
    assert w["cli"].main(["group-em-revoke", w["target"]]) == 0
    capsys.readouterr()
    assert w["calls"][-1] == f"revoke {w['target']}"
    assert not _relayed_file(w).exists()
    code, out = _check(w, capsys)
    assert code != 0 and "revoked" in out and "basis:" not in out


def test_unsigned_grant_is_refused_on_delivery_and_reads_unsigned_when_stored(world, capsys):
    w = world
    assert _grant(w, capsys)[0] == 0
    box = member_box.resolve_member_box(w["ids"]["b"], home=w["home_a"])["box"]
    _as(w, "a", HOLDER)
    good = json.loads(_relayed_file(w).read_text(encoding="utf-8"))["envelope"]
    unsigned = {k: v for k, v in good.items() if k != "sig"}
    reply = box_transport.deliver_grant(box, unsigned, runner=w["runner"])
    assert reply["ok"] is False and reply["reason"].startswith("bad_signature:unsigned")
    _edit_relayed(w, lambda env: env.pop("sig"))
    code, out = _check(w, capsys)
    assert code != 0 and "unsigned" in out and "basis:" not in out


def test_forged_tag_reads_forged_and_a_foreign_issuer_never_reaches_receive(world, capsys):
    w = world
    assert _grant(w, capsys)[0] == 0
    sig = json.loads(_relayed_file(w).read_text(encoding="utf-8"))["envelope"]["sig"]
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    _edit_relayed(w, lambda env: env.__setitem__("sig", flipped))
    code, out = _check(w, capsys)
    assert code != 0 and "forged" in out and "basis:" not in out

    box = member_box.resolve_member_box(w["ids"]["b"], home=w["home_a"])["box"]
    _relayed_file(w).unlink()
    _as(w, "a", HOLDER)
    issued = suite_grant.time.time()
    evil = box_key.sign(
        {
            "schema_version": suite_grant.SCHEMA_VERSION, "kind": suite_grant.KIND,
            "issuer_session_id": HOLDER, "issuer_name": "", "issuer_box_id": "box-evil0009",
            "grantee_session_id": PEER, "grantee_box_id": w["ids"]["b"], "grantee_name": "",
            "issued_at": suite_grant._iso(issued), "expires_at": suite_grant._iso(issued + 600),
            "note": "ruling", "issuer_key_fp": box_key.own_fingerprint(w["home_a"]),
        },
        w["home_a"],
    )
    reply = box_transport.deliver_grant(box, evil, runner=w["runner"])
    assert reply == {"ok": False, "reason": "issuer_not_peer"}
    assert not _relayed_file(w).exists()


def test_a_pre_pinned_issuer_key_is_never_overwritten(world, capsys):
    w = world
    other = base64.b64encode(b"s" * 32).decode()
    box_key.pin_peer_key(w["ids"]["a"], other, w["home_b"])
    pinned = box_key.pinned_fingerprint(w["ids"]["a"], w["home_b"])
    code, out = _grant(w, capsys)
    assert code != 0 and "pin_mismatch" in out
    assert not any(c.startswith("keypin") for c in w["calls"])
    assert not _relayed_file(w).exists()
    a_key = base64.b64encode(box_key.ensure_key(w["home_a"])).decode()
    again = relayed_grant_peer.receive_key_pin(w["ids"]["a"], a_key, w["ids"]["a"], w["home_b"])
    assert again == {"ok": False, "reason": "pin_exists"}
    assert box_key.pinned_fingerprint(w["ids"]["a"], w["home_b"]) == pinned
