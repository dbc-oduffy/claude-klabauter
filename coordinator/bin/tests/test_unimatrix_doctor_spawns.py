"""Behavior pins for unimatrix-doctor: real ssh-keygen verify over tmp settings homes, injected network probes."""
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required"), pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


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
        self.v6, self.v6_only = {}, {}  # host -> fe80 address resolved alongside / instead of the v4 one
        self.dialled = []
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
        if host in self.v6:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (self.v6[host], 22, 0, 3)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 22))]
        if host in self.v6_only:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (self.v6_only[host], 22, 0, 3))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 22))]

    def connect(self, ep, timeout):
        self.dialled.append(ep)
        if len(ep) != 2 or "%" in ep[0]:
            raise OverflowError("getsockaddrarg: flowinfo/scope-bearing sockaddr")
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


LAUNCHCTL_BOTH = "-\t0\tcom.unimatrix.autolink\n-\t0\tcom.coordinator.unimatrix-autolink\n123\t0\tcom.apple.Finder\n"


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


MIRROR = "machine-b-em-on-machine-a"
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DIGEST = "ab" * 32


def test_confirm_at_default_arguments_on_an_empty_box_exits_nonzero(box, tmp_path):
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-doctor.py"), "confirm", MIRROR], capture_output=True,
                       text=True, env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(box.home),
                                       "HOME": str(tmp_path / "h"), "USERPROFILE": str(tmp_path / "h")},
                       cwd=str(tmp_path))
    assert r.returncode == 1 and r.stdout.startswith("UNCONFIRMED: no mirror")


def test_cli_entrypoint_exits_non_zero_on_a_box_with_no_snapshot(tmp_path):
    home = tmp_path / "s"
    home.mkdir()
    ssh = tmp_path / "h" / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "authorized_keys").write_text('restrict,command="python3 /x/unimatrix-relay.py" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabcdefgh u\n')
    import os
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-doctor.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(home), "HOME": str(tmp_path / "h"),
                            "USERPROFILE": str(tmp_path / "h"),
                            "ProgramData": str(tmp_path / "pd")})  # no sshd_config there: the user file is the effective one
    assert r.returncode == 1
    assert "problem: no signed membership snapshot is stored" in r.stdout and "is in no signed membership record" in r.stdout
