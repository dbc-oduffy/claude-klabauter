"""Behavior pins for unimatrix-doctor: real ssh-keygen verify over tmp settings homes, injected network probes."""
import importlib.util
import io
import json
import os
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
        self.wrong_host = set()  # ssh targets whose host key does not match HostKeyAlias
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
        if remote in self.wrong_host:
            return subprocess.CompletedProcess(cmd, 255, b"", b"Host key verification failed.")
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
    monkeypatch.setattr(al, "pin_key_value", lambda r, run=subprocess.run: sys.executable)

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
        kw.setdefault("launchctl_list", lambda: "")
        kw.setdefault("relay_ps", lambda: "")
        kw.setdefault("environ", {"PATH": ""})
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


def test_reserved_ip_pin_is_accepted_and_an_undeclared_one_is_still_flagged(box):
    (box.home / "bin" / "_machine_local.py").write_text(
        "import sys\nk = sys.argv[-1]\n"
        "print({'unimatrix.far_boxes': 'resbox,ipbox', 'unimatrix.reserved_ips': '10.0.0.116, 10.0.0.174'}.get(k, ''))\n"
        "sys.exit(0 if k in ('unimatrix.far_boxes', 'unimatrix.reserved_ips') else 1)\n")
    box.net.alias_host = {"resbox": "10.0.0.116", "ipbox": "192.168.1.9"}
    out = box.main()[1]
    assert "resbox: far_boxes alias is pinned to IP" not in out
    assert "ipbox: far_boxes alias is pinned to IP 192.168.1.9" in out


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
    assert "nodns: no relay key" not in out and "nodns: relay key" not in out and "nodns: peer" not in out


def _lists(**by_key):
    return lambda key: by_key.get(key, "")


def test_primary_name_down_but_a_listed_address_answers_is_not_a_problem(box):
    box.adopt("me", "hop")
    box.net.dead_dns = {"hop.local"}
    box.net.refuse = {"10.0.0.1"}
    rc, out = box.main(read_key=_lists(**{"unimatrix.addresses.hop": "10.0.0.1, 10.0.0.2"}))
    assert "hop: hop.local does not resolve" not in out
    assert "box answers at 10.0.0.2" in out
    assert any(c[-2:] == ["kim@10.0.0.2", "roster"] and "HostKeyAlias=hop" in c and "StrictHostKeyChecking=yes" in c
               for c in box.net.cmds)


def test_listed_address_held_by_another_machine_is_not_this_box_and_never_accepted(box):
    box.adopt("me", "hop")
    box.net.dead_dns = {"hop.local"}
    box.net.wrong_host = {"kim@10.0.0.3"}
    rc, out = box.main(read_key=_lists(**{"unimatrix.addresses.hop": "10.0.0.3"}))
    assert rc == 1
    assert "hop: hop.local does not resolve" in out
    assert "10.0.0.3 answers with a different host key (not this box)" in out
    assert "box answers at" not in out


def test_primary_down_with_no_address_list_keeps_the_original_problem_unchanged(box):
    box.adopt("me", "hop")
    box.net.refuse = {"hop.local"}
    rc, out = box.main(read_key=_lists())
    assert rc == 1
    assert "hop: hop.local refuses TCP 22" in out and "not this box" not in out
    assert not any("HostKeyAlias=hop" in c for c in box.net.cmds)


def test_pinned_hostkey_must_match_the_known_hosts_entry_for_the_box_alias(box, tmp_path):
    box.adopt("me", "hop")
    kh_key = keygen(tmp_path / "hostkey")[1]
    fp = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "hostkey.pub")], capture_output=True, text=True,
                        check=True).stdout.split()[1]
    kh = tmp_path / "known_hosts"
    kh.write_text(f"hop {kh_key}\n")
    good = box.main(read_key=_lists(**{"unimatrix.hostkey.hop": fp}), known_hosts=kh)[1]
    assert "HostKeyAlias hop" not in good
    bad = box.main(read_key=_lists(**{"unimatrix.hostkey.hop": "SHA256:notthekey"}), known_hosts=kh)[1]
    assert "known_hosts key for HostKeyAlias hop" in bad and "differs from the pinned SHA256:notthekey" in bad
    kh.write_text("")
    missing = box.main(read_key=_lists(**{"unimatrix.hostkey.hop": fp}), known_hosts=kh)[1]
    assert "no known_hosts entry for HostKeyAlias hop" in missing


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


def test_each_far_box_prints_a_bridge_and_an_inbox_line_with_pid_when_alive_and_remedy_when_dead(box):
    snap = box.adopt("me", "machine-a", "bishop")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap), "kim@bishop.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert "info: machine-a: bridge supervisor (unimatrix-roster.py) alive, pid 7001" in out
    assert "info: machine-a: inbox supervisor (unimatrix-mirror-all.py) alive, pid 7002" in out
    assert "machine-a: bridge supervisor" not in "".join(l for l in out.splitlines() if l.startswith("problem:"))
    dead = [l for l in out.splitlines() if l.startswith("problem: bishop:") and "supervisor" in l]
    assert len(dead) == 2 and all("is not alive (" in l and "remedy:" in l for l in dead)
    assert any("bridge" in l for l in dead) and any("inbox" in l for l in dead)


def test_unset_python_pin_is_a_problem_line_and_a_set_one_is_silent(box, monkeypatch):
    assert "unimatrix.python" not in box.main()[1]
    monkeypatch.setattr(box.al, "pin_key_value", lambda r, run=subprocess.run: None)
    out = box.main()[1]
    assert "problem: unimatrix.python is unset in machine-local" in out and "remedy:" in out
    monkeypatch.setattr(box.al, "pin_key_value", lambda r, run=subprocess.run: str(box.tmp / "nope.exe"))
    assert "names a missing interpreter" in box.main()[1]


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
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
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


def _lstart(epoch):
    import time
    t = time.localtime(epoch)
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"Mon {months[t.tm_mon - 1]} {t.tm_mday:2d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} {t.tm_year}"


def _ps(box, *rows, script="unimatrix-relay.py"):
    lines = [f"{pid} {ppid} {_lstart(epoch)} /usr/bin/python3 {BIN / script}" for pid, ppid, epoch in rows]
    return "\n".join(lines + [f"999 1 {_lstart(1_000_000_000)} /usr/sbin/sshd-session -R"])


LAUNCHCTL_BOTH = "-\t0\tcom.unimatrix.autolink\n-\t0\tcom.coordinator.unimatrix-autolink\n123\t0\tcom.apple.Finder\n"


def test_both_autolink_labels_loaded_is_one_problem_and_one_label_is_silent(box):
    rc, out = box.main(platform="darwin", launchctl_list=lambda: LAUNCHCTL_BOTH)
    assert "problem: two autolink labels are loaded in launchd" in out
    assert "remedy: run unimatrix-install.py --install" in out
    for one in ("-\t0\tcom.unimatrix.autolink\n", "-\t0\tcom.coordinator.unimatrix-autolink\n", ""):
        assert "two autolink labels" not in box.main(platform="darwin", launchctl_list=lambda one=one: one)[1]


def test_launchctl_is_not_consulted_off_macos(box):
    def boom():
        raise AssertionError("launchctl ran")

    assert "two autolink labels" not in box.main(platform="linux", launchctl_list=boom)[1]


def test_orphaned_and_replaced_code_relay_sessions_are_each_named_and_a_live_one_is_not(box, monkeypatch):
    stamp = (BIN / "unimatrix-relay.py").stat().st_ctime
    box.live.add(100)
    ps = _ps(box, (201, 1, stamp + 500), (202, 100, stamp - 90_000), (203, 100, stamp + 500))
    rc, out = box.main(platform="linux", relay_ps=lambda: ps)
    lines = [ln for ln in out.splitlines() if "relay session" in ln]
    assert len(lines) == 2
    assert "pid 201" in lines[0] and "orphaned" in lines[0] and "unimatrix-install.py --install" in lines[0]
    assert "pid 202" in lines[1] and "replaced" in lines[1]
    assert "pid 203" not in out


def test_relay_sessions_are_not_read_on_windows(box):
    def boom():
        raise AssertionError("ps ran")

    assert "relay session" not in box.main(platform="win32", relay_ps=boom)[1]


def _forced(box, interp):
    box.ak.write_text(f'restrict,command="{interp} /x/unimatrix/unimatrix-relay.py" ssh-ed25519 '
                      f'{box.relay[1].split()[1]} unimatrix\n')


def test_a_forced_interpreter_other_than_autolinks_pinned_one_is_a_split(box, tmp_path):
    pinned = box.al.pinned_python(box.al._roster())
    other = tmp_path / "pyenv" / "python3"
    _forced(box, other)
    split = [ln for ln in box.main(platform="linux")[1].splitlines() if "interpreter split" in ln]
    assert len(split) == 1 and str(other) in split[0] and str(pinned) in split[0]
    assert "unimatrix-install.py --install" in split[0]


def test_the_installs_rewrite_clears_the_split_the_doctor_reports(box, tmp_path):
    pinned = box.al.pinned_python(box.al._roster())
    _forced(box, tmp_path / "pyenv" / "python3")
    assert "interpreter split" in box.main(platform="linux")[1]
    assert load("unimatrix-install").rewrite_forced_interpreters(box.ak, pinned) == 1
    assert "interpreter split" not in box.main(platform="linux")[1]


def test_a_forced_interpreter_equal_to_the_pinned_one_or_a_bare_name_is_no_split(box):
    _forced(box, box.al.pinned_python(box.al._roster()))
    assert "interpreter split" not in box.main(platform="linux")[1]
    _forced(box, "python3")
    assert "interpreter split" not in box.main(platform="linux")[1]


def test_a_forced_interpreter_path_with_a_space_is_parsed_whole(box, tmp_path):
    spaced = tmp_path / "py env" / "python3"
    box.ak.write_text('restrict,command="\\"%s\\" \\"/x/unimatrix-relay.py\\"" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabcdefgh unimatrix\n' % spaced)
    assert box.dr.forced_interpreters(box.ak) == [str(spaced)]


def _pwsh_path(tmp_path, *, alias_first):
    alias = tmp_path / "WindowsApps"
    real = tmp_path / "ProgramFiles" / "PowerShell" / "7"
    for d in (alias, real):
        d.mkdir(parents=True)
        (d / "pwsh.exe").write_text("")
    order = [alias, real] if alias_first else [real, alias]
    return {"PATH": os.pathsep.join(map(str, order))}, alias


def test_alias_first_pwsh_on_windows_is_one_problem_naming_the_alias_and_the_remedy(box, tmp_path):
    environ, alias = _pwsh_path(tmp_path, alias_first=True)
    rc, out = box.main(platform="win32", environ=environ)
    lines = [l for l in out.splitlines() if "pwsh" in l]
    assert rc == 1 and len(lines) == 1
    assert str(alias / "pwsh.exe") in lines[0] and "PowerShell\\7" in lines[0]


def test_real_pwsh_first_on_windows_is_no_problem(box, tmp_path):
    environ, _ = _pwsh_path(tmp_path, alias_first=False)
    assert "pwsh" not in box.main(platform="win32", environ=environ)[1]


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_alias_first_pwsh_is_not_checked_off_windows(box, tmp_path, plat):
    environ, _ = _pwsh_path(tmp_path, alias_first=True)
    assert "pwsh" not in box.main(platform=plat, environ=environ)[1]


def test_a_clean_box_prints_none_of_the_three(box):
    _forced(box, box.al.pinned_python(box.al._roster()))
    out = box.main(platform="darwin", launchctl_list=lambda: "-\t0\tcom.unimatrix.autolink\n", relay_ps=lambda: "")[1]
    assert not any(k in out for k in ("two autolink labels", "relay session", "interpreter split"))


def _asym(out):
    return [ln for ln in out.splitlines() if "asymmetric:" in ln]


def test_pair_this_box_can_start_but_the_peer_cannot_is_asymmetric_and_exits_nonzero(box):
    snap = box.adopt("me", "machine-a")
    box.ak.write_text("")  # no forced line carries machine-a's relay key: machine-a cannot start a link to us
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main(platform="linux")  # win32 never judges inbound authorized_keys
    assert rc == 1
    assert _asym(out) == ["problem: asymmetric: machine-a -> me missing"]


def test_pair_the_peer_can_start_but_this_box_cannot_is_asymmetric(box):
    snap = box.adopt("me", "machine-a")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.net.roster_rc = {"kim@machine-a.local": 255}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert rc == 1
    assert _asym(out) == ["problem: asymmetric: me -> machine-a missing"]


def test_absent_outbound_relay_key_is_asymmetric(box):
    snap = box.adopt("me", "machine-a")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    gone = str(box.tmp / "no-such-key")
    box.al.resolve_key = lambda *a, **k: gone
    rc, out = box.main()
    assert rc == 1 and _asym(out) == ["problem: asymmetric: me -> machine-a missing"]


def test_pair_startable_from_both_sides_names_no_asymmetry(box):
    snap = box.adopt("me", "machine-a")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert rc == 0 and _asym(out) == []


def test_inbound_authorized_keys_is_not_judged_on_windows(box):
    box.adopt("me", "machine-a")
    box.ak.write_text("")
    assert _asym(box.main(platform="win32")[1]) == []


def test_default_cli_on_a_one_sided_fixture_prints_asymmetric_and_exits_nonzero(box, tmp_path):
    box.adopt("me", "machine-a")
    box.ak.write_text("")
    # The doctor never judges inbound authorized_keys on win32, so the platform is pinned off it; every other default is real.
    boot = ("import importlib.util, sys\n"
            "s = importlib.util.spec_from_file_location('unimatrix_doctor_cli', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(s)\n"
            "s.loader.exec_module(m)\n"
            "sys.exit(m.main([], platform='linux'))\n")
    r = subprocess.run([sys.executable, "-c", boot, str(BIN / "unimatrix-doctor.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(box.home), "HOME": str(tmp_path / "h"),
                            "USERPROFILE": str(tmp_path / "h")}, cwd=str(tmp_path))
    assert r.returncode == 1 and "asymmetric: " in r.stdout


def test_replaced_code_is_judged_against_the_installed_copy_not_the_checkout(box):
    box.sdir.mkdir(parents=True, exist_ok=True)
    installed = box.sdir / "unimatrix-relay.py"
    installed.write_text("x")
    stamp = installed.stat().st_ctime
    box.live.add(100)
    older = _ps(box, (301, 100, stamp - 90_000))
    newer = _ps(box, (302, 100, stamp + 500))
    assert "pid 301" in box.main(platform="linux", relay_ps=lambda: older)[1]
    assert "relay session" not in box.main(platform="linux", relay_ps=lambda: newer)[1]


def test_a_far_boxes_alias_reads_its_address_list_and_pin_under_its_box_id():
    dr = load("unimatrix-doctor")
    read = []
    ctx = {"read_key": lambda k: read.append(k) or "", "roster": None, "run": None}
    assert dr.address_fallback("me@192.0.2.33", "me@192.0.2.33", "k", ctx) == (None, [])
    assert dr.hostkey_problems("me@macmini.local", ctx) == []
    assert read == ["unimatrix.addresses.192.0.2.33", "unimatrix.hostkey.macmini"]


def test_stale_relay_session_remedy_names_the_checkout_install_command(box):
    box.sdir.mkdir(parents=True, exist_ok=True)
    installed = box.sdir / "unimatrix-relay.py"
    installed.write_text("x")
    box.live.add(100)
    older = _ps(box, (301, 100, installed.stat().st_ctime - 90_000))
    line = next(p for p in _problems(box.main(platform="linux", relay_ps=lambda: older)[1]) if "pid 301" in p)
    assert "<checkout>/coordinator/bin/unimatrix-install.py --install" in line


MIRROR = "machine-b-em-on-machine-a"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DIGEST = "ab" * 32


class FarRelay:
    """Injected channel: answers the `receipts` command like the far relay (or like an old one)."""

    def __init__(self, rows=(), old=False, rc=0):
        self.rows, self.old, self.rc, self.commands = list(rows), old, rc, []

    def __call__(self, remote, key):
        self.remote = remote
        return self

    def run(self, command, data=b"", timeout=30):
        self.commands.append(command)
        if self.old:
            return subprocess.CompletedProcess([], 1, b"", b"usage: roster | specs | send <pid> | ...")
        out = json.dumps({"ok": True, "rows": self.rows}).encode() + b"\n"
        return subprocess.CompletedProcess([], self.rc, out if not self.rc else b"", b"" if not self.rc else b"ssh: down")


def _confirm(box, far, delivery=None, entry=True):
    sessions = box.tmp / "sessions"
    sessions.mkdir(exist_ok=True)
    if entry:
        (sessions / "1.json").write_text(json.dumps(
            {"name": MIRROR, "sessionId": SID, "unimatrixMirror": True, "unimatrixRemote": "kim@machine-a.local"}))
    deliveries = box.tmp / "deliveries.jsonl"
    deliveries.unlink(missing_ok=True)
    if delivery is not None:
        row = {"ts": "t", "at": 10_000.0, "target": f"{MIRROR} (pid 9, session {SID})", "ok": True, "ms": 5}
        row.update(delivery)
        deliveries.write_text(json.dumps(row) + "\n")
    box.al.resolve_key = lambda *a, **k: "key"
    out = io.StringIO()
    rc = box.dr.main(["confirm", MIRROR], run=box.net.run, out=out, roster=box.al._roster(), autolink=box.al,
                     sessions_dir=sessions, deliveries=deliveries, channel=far)
    return rc, out.getvalue()


def test_confirm_prints_confirmed_when_the_far_relay_holds_the_digest(box):
    far = FarRelay(rows=[{"session": SID, "digest": DIGEST}])
    rc, out = _confirm(box, far, {"digest": DIGEST})
    assert rc == 0 and out.startswith("CONFIRMED")
    # since = the delivery's `at` minus 300 s, for the two boxes' unsynchronised clocks
    assert far.commands == [f"receipts {SID} 9700"] and far.remote == "kim@machine-a.local"


def test_confirm_prints_unconfirmed_when_the_far_relay_lacks_the_digest(box):
    rc, out = _confirm(box, FarRelay(rows=[{"session": SID, "digest": "cd" * 32}]), {"digest": DIGEST})
    assert rc == 1 and out.startswith("UNCONFIRMED") and "no receipt" in out


def test_confirm_names_a_row_without_a_digest_as_a_pre_receipt_bridge(box):
    far = FarRelay(rows=[{"session": SID, "digest": DIGEST}])
    rc, out = _confirm(box, far, {})
    assert rc == 1 and out.startswith("UNCONFIRMED") and "digest" in out and far.commands == []


def test_confirm_does_not_confirm_a_failed_delivery(box):
    far = FarRelay(rows=[{"session": SID, "digest": DIGEST}])
    rc, out = _confirm(box, far, {"ok": False, "error": "boom", "digest": DIGEST})
    assert rc == 1 and "failed" in out and far.commands == []


def test_confirm_on_an_old_far_relay_says_to_upgrade_and_exits_nonzero(box):
    rc, out = _confirm(box, FarRelay(old=True), {"digest": DIGEST})
    assert rc == 1 and out.startswith("UNCONFIRMED: the far relay lacks receipts; upgrade it")


def test_confirm_with_an_unreachable_far_relay_is_unconfirmed(box):
    rc, out = _confirm(box, FarRelay(rc=255), {"digest": DIGEST})
    assert rc == 1 and out.startswith("UNCONFIRMED") and "did not answer" in out


def test_confirm_with_no_mirror_or_no_delivery_is_unconfirmed(box):
    assert _confirm(box, FarRelay(), {"digest": DIGEST}, entry=False)[1].startswith("UNCONFIRMED: no mirror")
    assert _confirm(box, FarRelay(), None)[1].startswith("UNCONFIRMED: no delivery")


def test_confirm_at_default_arguments_on_an_empty_box_exits_nonzero(box, tmp_path):
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-doctor.py"), "confirm", MIRROR], capture_output=True,
                       text=True, env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(box.home),
                                       "HOME": str(tmp_path / "h"), "USERPROFILE": str(tmp_path / "h")},
                       cwd=str(tmp_path))
    assert r.returncode == 1 and r.stdout.startswith("UNCONFIRMED: no mirror")


def _pair_snapshot(box):
    """Epoch 2 with `machine-a` carrying relay_pub=other and relay_pubs={me: relay}; returns the snapshot."""
    m = box.al._membership()
    box.mem = m
    m.write_pin(box.home, box.signer[1])
    me = {"box": "me", "host": "me.local", "user": "kim", "relay_pub": box.other[1], "from_cidrs": []}
    peer = {"box": "machine-a", "host": "machine-a.local", "user": "kim", "relay_pub": box.relay[1],
            "relay_pubs": {"me": box.pair[1]}, "from_cidrs": []}
    snap = None
    for member in (me, peer):
        snap = m.next_snapshot(snap, {"op": "join", "member": member})
        m.adopt(box.home, snap, m.FakeSigner(box.signer[0], environ={}).sign(m.canonical_bytes(snap)),
                notifier=lambda *a, **k: None)
    return snap


def _problems(out):
    return [ln for ln in out.splitlines() if ln.startswith("problem:")]


def test_the_per_pair_key_addressed_to_this_box_signs_its_line_and_satisfies_the_inbound_check(box):
    box.pair = keygen(box.tmp / "pair")
    snap = _pair_snapshot(box)
    box.ak.write_text(box.relay_line(box.pair[1], "machine-a") + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert (rc, _problems(out)) == (0, [])


def test_a_missing_per_pair_key_is_one_inbound_problem_even_when_the_members_other_key_is_held(box):
    box.pair = keygen(box.tmp / "pair")
    snap = _pair_snapshot(box)
    box.ak.write_text(box.relay_line(box.relay[1], "machine-a") + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main(platform="linux")  # win32 never judges inbound authorized_keys
    assert rc == 1
    assert [p for p in _problems(out) if "asymmetric: machine-a -> me missing" in p] != []
    assert not [p for p in _problems(out) if "no signed membership record" in p]


def test_no_pin_is_a_problem_naming_the_pin_command_and_a_held_pin_still_prints_its_fingerprint(box):
    rc, out = box.main()
    assert rc == 1 and any("no signer pin" in p and "unimatrix-pair.py pin" in p for p in _problems(out))
    snap = box.adopt("me", "machine-a")
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    rc, out = box.main()
    assert "info: pin SHA256:" in out and not any("no signer pin" in p for p in _problems(out))


def _far_boxes(box, *aliases):
    reg = "import sys\nk = sys.argv[-1]\nprint(' '.join(%r) if k == 'unimatrix.far_boxes' else '')\nsys.exit(0 if k == 'unimatrix.far_boxes' else 1)\n"
    (box.home / "bin" / "_machine_local.py").write_text(reg % (list(aliases),))


def _ctx(box):
    return {"autolink": box.al, "roster": box.al._roster(), "run": box.net.run}


def test_relay_key_toward_a_member_uses_the_ssh_alias_whose_hostname_matches_its_host(box, monkeypatch):
    _far_boxes(box, "macmini-lan", "macmini")
    box.net.alias_host = {"macmini-lan": "mac-mini.local", "macmini": "100.64.0.9"}
    asked = []
    monkeypatch.setattr(box.al, "resolve_key", lambda roster, alias, **kw: asked.append(alias) or f"/k/{alias}")
    ctx = _ctx(box)
    assert box.dr.relay_key(ctx, "Mac-mini", "Mac-mini.local") == "/k/macmini-lan"
    assert box.dr.relay_key(ctx, "Mac-mini") == "/k/Mac-mini"
    assert box.dr.relay_key(ctx, "bishop", "bishop.local") == "/k/bishop"
    assert asked == ["macmini-lan", "Mac-mini", "bishop"]


def test_doctor_dials_a_member_with_the_alias_key_not_the_box_name_key(box, monkeypatch):
    snap = box.adopt("me", "Mac-mini")
    _far_boxes(box, "macmini-lan")
    box.net.alias_host = {"macmini-lan": "mac-mini.local"}
    box.ak.write_text(box.relay_line(box.relay[1]) + "\n")
    box.net.peer_snapshot = {"kim@Mac-mini.local": peer_blob(box, snap)}
    box.seed_live("kim@Mac-mini.local")
    seen = []
    monkeypatch.setattr(box.al, "resolve_key", lambda roster, alias, **kw: seen.append(alias) or box.relay[0])
    rc, out = box.main()
    assert seen == ["macmini-lan"] and rc == 0, out


def test_unsigned_forced_key_names_its_owner_and_the_approve_or_revoke_remedy_and_is_never_pruned(box):
    snap = box.adopt("me", "machine-a")
    body = box.other[1].split()[1]
    box.ak.write_text(f'restrict,command="python3 /x/unimatrix/unimatrix-relay.py --peer a" ssh-ed25519 {body} machine-c-lan\n')
    box.net.peer_snapshot = {"kim@machine-a.local": peer_blob(box, snap)}
    box.seed_live("kim@machine-a.local")
    before = box.ak.read_text()
    rc, out = box.main()
    line = next(p for p in _problems(out) if "no signed membership record" in p)
    assert "owner: machine-c-lan" in line and "unimatrix-pair.py approve" in line and "unimatrix-pair.py revoke" in line
    assert box.ak.read_text() == before


def test_every_keyed_doctor_dial_forces_ipv4(box):
    _, cmds = _drift_problems(box, {"entry": {}, "relay_sha": box.dr.local_relay_sha()})
    dials = [c for c in cmds if c[:1] == ["ssh"] and "-G" not in c]
    assert dials and all("AddressFamily=inet" in c for c in dials)
