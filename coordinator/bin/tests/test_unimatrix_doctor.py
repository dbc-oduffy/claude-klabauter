"""Behavior pins for unimatrix-doctor: real ssh-keygen verify over tmp settings homes, injected network probes."""
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required"),
    pytest.mark.complete,
]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text().strip()


class Net:
    """Injected run/resolve/connect: records calls, answers per-host from the sets, passes other commands through."""

    def __init__(self):
        self.dead_dns, self.refuse, self.roster_rc = set(), set(), {}
        self.roster_out = {}  # remote -> bytes the far `roster` prints
        self.alias_host = {}
        self.peer_snapshot = {}  # remote -> bytes returned by `membership get`
        self.cmds = []
        self.kex, self.scan_ok, self.timeout = set(), set(), set()
        self.far_log = {}  # remote -> bytes the unprivileged far-log read returns

    def run(self, cmd, **kw):
        self.cmds.append(cmd)
        if cmd[0] != "ssh":
            return subprocess.run(cmd, **kw)
        if cmd[1] == "-G":
            host = self.alias_host.get(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0 if host else 255, f"hostname {host}\n" if host else "", "")
        if "ssh-keyscan" in cmd:
            ok = cmd[-7] in self.scan_ok
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, b"host ssh-ed25519 AAAA\n" if ok else b"", b"")
        if cmd[-1].startswith("journalctl"):
            log = self.far_log.get(cmd[-2])
            return subprocess.CompletedProcess(cmd, 0 if log else 1, log or b"", b"")
        remote = cmd[-3] if cmd[-2:] == ["membership", "get"] else cmd[-2]
        if remote in self.kex:
            return subprocess.CompletedProcess(
                cmd, 255, b"", b"kex_exchange_identification: Connection closed by remote host")
        if cmd[-2:] == ["membership", "get"]:
            remote = cmd[-3]
            blob = self.peer_snapshot.get(remote)
            return subprocess.CompletedProcess(cmd, 0 if blob else 1, blob or b"", b"")
        rc = self.roster_rc.get(remote, 0)
        return subprocess.CompletedProcess(cmd, rc, self.roster_out.get(remote, b""), b"Permission denied" if rc else b"")

    def resolve(self, host, port):
        if host in self.dead_dns:
            raise OSError("nodename nor servname provided")
        return [("addr",)]

    def connect(self, ep, timeout):
        if ep[0] in self.timeout:
            raise TimeoutError("timed out")
        if ep[0] in self.refuse:
            raise ConnectionRefusedError("refused")

        class S:
            def close(self):
                pass

        return S()


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.delenv("UNIMATRIX_TUNNEL_REMOTE", raising=False)
    dr = load("unimatrix-doctor")
    al = dr._load("unimatrix_autolink", "unimatrix-autolink.py")
    roster = al._roster()
    live = set()
    liveness = al._liveness()
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(roster, "proc_start", lambda pid: f"start-{pid}")

    class B:
        pass

    b = B()
    b.al = al
    b.dr, b.home, b.tmp, b.live, b.net = dr, home, tmp_path, live, Net()
    b.sdir = home / "unimatrix"
    b.ak = tmp_path / "authorized_keys"
    b.signer = keygen(tmp_path / "signer")
    b.relay = keygen(tmp_path / "relay")
    b.other = keygen(tmp_path / "other")
    (home / ".secrets" / "ssh").mkdir(parents=True)
    (home / ".secrets" / "ssh" / "unimatrix-zero").write_text("k")
    b.mem = None

    def main(**kw):
        out = io.StringIO()
        rc = dr.main([], run=b.net.run, resolve=b.net.resolve, connect=b.net.connect, authorized_keys=b.ak,
                     self_names=kw.pop("self_names", {"me"}), out=out, roster=roster, autolink=al, **kw)
        return rc, out.getvalue()

    b.main = main

    def adopt(*boxes, key=None):
        m = b.mem or al._membership()
        b.mem = m
        if m.load_pin(home) is None:
            m.write_pin(home, b.signer[1])
        snap = m.load_snapshot(home)[0]
        for name in boxes:
            member = {"box": name, "host": f"{name}.local", "user": "kim", "relay_pub": b.relay[1], "from_cidrs": []}
            snap = m.next_snapshot(snap, {"op": "join", "member": member})
            sig = m.FakeSigner(key or b.signer[0], environ={}).sign(m.canonical_bytes(snap))
            m.adopt(home, snap, sig, notifier=lambda *a, **k: None)
        return snap

    b.adopt = adopt

    def relay_line(pub, peer="alpha"):
        body = pub.split()[1]
        return f'restrict,command="python3 /x/unimatrix/unimatrix-relay.py --peer {peer}" ssh-ed25519 {body} unimatrix'

    b.relay_line = relay_line

    def seed_live(remote):
        b.sdir.mkdir(parents=True, exist_ok=True)
        slug = remote.replace("@", "_at_")
        b.live.update({7001, 7002})
        (b.sdir / f"autolink-{slug}.json").write_text(json.dumps(
            {"bridge": {"pid": 7001, "procStart": "start-7001"}, "inbox": {"pid": 7002, "procStart": "start-7002"}}))

    b.seed_live = seed_live
    return b


def peer_blob(box, snap):
    m = box.mem
    sig = (box.sdir / "membership" / "snapshot.sig").read_bytes()
    import base64
    return m.canonical_bytes(snap) + b"\n" + base64.b64encode(sig) + b"\n"


def test_default_run_flags_unsigned_line_dead_alias_and_ip_pinned_alias_and_builds_no_signer(box, monkeypatch):
    mem = box.al._membership()

    def refuse(*a, **kw):
        raise AssertionError("a signer was constructed")

    monkeypatch.setattr(mem, "FakeSigner", refuse)
    monkeypatch.setattr(mem, "AgentSigner", refuse)
    box.ak.write_text("# comment\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIkeep other-line\n" + box.relay_line(box.relay[1]) + "\n")
    (box.home / "bin" / "_machine_local.py").write_text(
        "import sys\nprint('deadbox,ipbox' if sys.argv[-1] == 'unimatrix.far_boxes' else '')\nsys.exit(0 if sys.argv[-1] == 'unimatrix.far_boxes' else 1)\n")
    box.net.alias_host = {"deadbox": "deadbox.lan", "ipbox": "192.168.1.9"}
    box.net.dead_dns = {"deadbox.lan"}
    rc, out = box.main()
    assert rc == 1
    assert "authorized_keys line 3" in out and "is in no signed membership record" in out
    assert "line 2" not in out  # a line that does not force the relay is not unimatrix's
    assert "deadbox: deadbox.lan does not resolve" in out
    assert "ipbox: far_boxes alias is pinned to IP 192.168.1.9" in out
    assert "no signed membership snapshot is stored" in out
    assert "remedy: run unimatrix-pair.py approve <target>" in out
    assert "remedy: point the alias at a hostname" in out
    assert all(line.startswith(("problem:", "info:")) for line in out.splitlines())


def test_signed_line_and_healthy_link_exit_zero_and_print_the_pin_fingerprint(box):
    snap = box.adopt("me", "machine-a")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert (rc, [ln for ln in out.splitlines() if ln.startswith("problem:")]) == (0, [])
    assert "info: pin SHA256:" in out


def test_line_whose_key_is_not_a_member_is_unsigned_even_with_a_valid_snapshot(box):
    snap = box.adopt("me", "machine-a")
    box.ak.write_text(box.relay_line(box.other[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert rc == 1 and "line 1" in out and "no signed membership record" in out


def test_unverifiable_snapshot_is_one_problem_and_every_relay_line_is_unsigned(box):
    box.adopt("me")
    m = box.mem
    forged = m.next_snapshot(m.load_snapshot(box.home)[0], {"op": "revoke", "box": "me"})
    d = box.sdir / "membership"
    (d / "snapshot.json").write_bytes(m.canonical_bytes(forged))
    (d / "verified.json").unlink()
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    rc, out = box.main()
    assert rc == 1
    assert "nothing linked from it" in out and "line 1" in out


def test_each_failed_leg_of_a_link_is_named_and_stops_the_later_legs(box):
    box.adopt("me", "nodns", "norefuse", "noroster")
    box.net.dead_dns = {"nodns.local"}
    box.net.refuse = {"norefuse.local"}
    box.net.roster_rc = {"kim@noroster.local": 255}
    rc, out = box.main()
    assert rc == 1
    assert "nodns: nodns.local does not resolve" in out
    assert "norefuse: norefuse.local refuses TCP 22" in out
    assert "noroster: relay key does not answer `roster`" in out and "rc=255" in out
    assert "nodns: " in out and "nodns: bridge" not in out


def _link_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("problem: mac:")]


def test_kex_closed_with_no_other_source_reaching_is_probable_unreachable_upstream(box):
    box.adopt("me", "mac")
    box.net.kex = {"kim@mac.local"}
    rc, out = box.main()
    line = _link_lines(out)[0]
    assert rc == 1 and "unreachable-upstream (probable)" in line
    assert "remedy: check that box's network link and prefer wired" in line
    assert "refusing-one-source" not in out and "sshd-refusing-all-sources" not in out


def test_tcp_timeout_with_readable_far_log_showing_no_spawn_is_definite_unreachable_upstream(box):
    box.adopt("me", "mac")
    box.net.timeout = {"mac.local"}
    rc, out = box.main(far_sshd_log=lambda remote, host: "sshd started\n")
    line = _link_lines(out)[0]
    assert "unreachable-upstream (definite)" in line and "prefer wired" in line


def test_kex_closed_while_far_log_shows_spawns_is_sshd_refusing_all_sources(box):
    box.adopt("me", "mac")
    box.net.kex = {"kim@mac.local"}
    rc, out = box.main(far_sshd_log=lambda remote, host: "Connection from 10.0.0.2 port 5\nConnection closed by 10.0.0.2\n")
    line = _link_lines(out)[0]
    assert "sshd-refusing-all-sources" in line and "restart sshd on that box" in line
    assert "unreachable-upstream" not in line and "prefer wired" not in line


def test_default_far_log_reader_uses_the_tunnel_route_when_the_direct_link_fails(box, monkeypatch):
    box.adopt("me", "mac")
    monkeypatch.setenv("UNIMATRIX_TUNNEL_REMOTE", "mac-tun")
    box.net.kex = {"kim@mac.local"}
    box.net.far_log = {"mac-tun": b"kex_exchange_identification: Connection closed by 10.0.0.2 port 5\n"}
    line = _link_lines(box.main()[1])[0]
    assert "sshd-refusing-all-sources" in line and "unreachable-upstream" not in line
    box.net.far_log = {"mac-tun": b"sshd started\nAccepted publickey for kim from 10.77.0.2\n"}
    assert "unreachable-upstream (definite)" in _link_lines(box.main()[1])[0]


def test_default_far_log_reader_direct_only_is_probable_since_the_failing_link_cannot_read_its_own_log(box):
    box.adopt("me", "mac")
    box.net.kex = {"kim@mac.local"}
    box.net.far_log = {}
    assert "unreachable-upstream (probable)" in _link_lines(box.main()[1])[0]


def test_kex_closed_while_another_source_reaches_port_22_is_refusing_one_source(box):
    box.adopt("me", "mac", "rook")
    box.net.kex = {"kim@mac.local"}
    box.net.scan_ok = {"kim@rook.local"}
    rc, out = box.main()
    line = _link_lines(out)[0]
    assert "refusing-one-source" in line and "another source reaches it" in line
    assert "unreachable-upstream" not in line and "sshd-refusing-all-sources" not in line


def test_peer_that_does_not_list_us_or_holds_another_epoch_is_flagged(box):
    snap1 = box.adopt("me", "machine-a")
    box.seed_live("kim@machine-a.local")
    m = box.mem
    other_pov = dict(snap1, members=[x for x in snap1["members"] if x["box"] != "me"])
    box.net.peer_snapshot = {"kim@machine-a.local": m.canonical_bytes(other_pov) + b"\nAAAA\n"}
    rc, out = box.main()
    assert rc == 1 and "machine-a: peer's snapshot does not list this box" in out
    behind = dict(snap1, epoch=snap1["epoch"] - 1)
    box.net.peer_snapshot = {"kim@machine-a.local": m.canonical_bytes(behind) + b"\nAAAA\n"}
    rc, out = box.main()
    assert f"peer holds snapshot epoch {snap1['epoch'] - 1}, this box holds {snap1['epoch']}" in out


def _drift_problems(box, row):
    snap = box.adopt("me", "machine-a")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    box.net.roster_out = {"kim@machine-a.local": (json.dumps(row) + "\n").encode()}
    rc, out = box.main()
    return [ln for ln in out.splitlines() if "relay version drift" in ln], box.net.cmds


def test_far_relay_sha_differing_from_local_is_named_on_the_same_roster_round_trip(box):
    local = box.dr.local_relay_sha()
    assert local
    lines, cmds = _drift_problems(box, {"entry": {}, "relay_sha": "deadbeef0000"})
    assert len(lines) == 1 and "machine-a" in lines[0] and "deadbeef0000" in lines[0] and local in lines[0]
    assert sum(1 for c in cmds if c[:1] == ["ssh"] and c[-1] == "roster") == 1


def test_far_relay_with_matching_sha_is_silent(box):
    lines, _ = _drift_problems(box, {"entry": {}, "relay_sha": box.dr.local_relay_sha()})
    assert lines == []


def test_far_relay_row_without_relay_sha_is_drift_and_an_empty_roster_is_not(box):
    lines, _ = _drift_problems(box, {"entry": {}})
    assert len(lines) == 1 and "predates relay_sha" in lines[0]
    assert box.dr.relay_drift("x", b"") == []


def test_relay_roster_rows_carry_the_sha_doctor_compares(capsys, monkeypatch):
    relay = load("unimatrix-relay")
    assert relay.relay_sha() == load("unimatrix-doctor").local_relay_sha()


def test_peer_without_a_snapshot_and_dead_supervisors_are_named(box):
    box.adopt("me", "machine-a")
    rc, out = box.main()
    assert rc == 1
    assert "machine-a: peer holds no membership snapshot" in out
    assert "machine-a: bridge supervisor is not alive" in out and "machine-a: inbox supervisor is not alive" in out


def test_signed_member_with_no_live_link_is_named_and_a_live_one_is_not(box):
    snap = box.adopt("me", "machine-a", "bishop")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap), "kim@bishop.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert rc == 1
    assert "bishop: no live link path" in out
    assert "machine-a: no live link path" not in out


def test_signed_member_autolink_skips_is_named_as_having_no_path(box, monkeypatch):
    snap = box.adopt("me", "machine-a", "bishop")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    monkeypatch.setattr(box.al, "ALIAS_RE", re.compile(r"[a-z@.]+machine-a\.local"))
    rc, out = box.main()
    assert rc == 1
    assert "bishop: no live link path: dial name 'kim@bishop.local'" in out
    assert "machine-a: no live link path" not in out


def test_far_boxes_alias_is_not_checked_when_a_snapshot_is_stored_and_still_is_when_none(box):
    far = "import sys\nprint('ghost' if sys.argv[-1] == 'unimatrix.far_boxes' else '')\nsys.exit(0 if sys.argv[-1] == 'unimatrix.far_boxes' else 1)\n"
    (box.home / "bin" / "_machine_local.py").write_text(far)
    box.net.alias_host = {}
    box.ak.write_text("")
    rc, out = box.main()
    assert "ghost: far_boxes alias does not resolve" in out  # no snapshot: autolink dials it, so it is checked
    snap = box.adopt("me", "machine-a")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert rc == 0 and "ghost" not in out  # a snapshot makes far_boxes inert, never a flagged member


def test_windows_admin_authorized_keys_is_reported_never_read(box, tmp_path, monkeypatch):
    prog = tmp_path / "ProgramData"
    (prog / "ssh").mkdir(parents=True)
    admin = prog / "ssh" / "administrators_authorized_keys"
    admin.write_text(box.relay_line(box.relay[1]) + "\n")
    monkeypatch.setenv("ProgramData", str(prog))
    box.adopt("me")
    box.ak.write_text("")
    rc, out = box.main(platform="win32")
    assert rc == 0 and f"info: {admin} exists and is not inspected here" in out
    assert "line 1" not in out


def test_cli_entrypoint_exits_non_zero_on_a_box_with_no_snapshot(tmp_path):
    home = tmp_path / "s"
    home.mkdir()
    ssh = tmp_path / "h" / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "authorized_keys").write_text('restrict,command="python3 /x/unimatrix-relay.py" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabcdefgh u\n')
    import os
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-doctor.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(home), "HOME": str(tmp_path / "h"),
                            "USERPROFILE": str(tmp_path / "h")})
    assert r.returncode == 1
    assert "problem: no signed membership snapshot is stored" in r.stdout and "is in no signed membership record" in r.stdout


@pytest.mark.parametrize("verdict, needle", [("locked", "waiting: 1Password locked"),
                                            ("missing", "passphrase_source_missing")])
def test_peer_check_names_an_unloadable_encrypted_key_without_dialling(box, verdict, needle):
    class AL:
        @staticmethod
        def agent_state(key, run=None):
            return verdict

    def no_dial(cmd, **kw):
        raise AssertionError("no ssh dial for a key the agent cannot hold")

    ctx = {"run": no_dial, "roster": box.al._roster(), "autolink": AL}
    probs = box.dr.peer_lists_us(ctx, {"epoch": 1}, {"me"})("peer", "/k", "alpha")
    assert len(probs) == 1 and needle in probs[0] and probs[0].startswith("alpha:")
