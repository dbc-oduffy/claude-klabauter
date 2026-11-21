"""Behavior pins for the unimatrix far-box autolink launcher: ssh and process spawns are faked."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    engine = tmp_path / "engine"
    (engine / "coordinator_core").mkdir(parents=True)
    (engine / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(engine))
    monkeypatch.delenv("UNIMATRIX_TUNNEL_REMOTE", raising=False)
    monkeypatch.setenv("UNIMATRIX_AUTOLINK_NO_ROUTER", "1")
    monkeypatch.setenv("UNIMATRIX_TEST_ALLOWED_BOXES", "machine-a a@b fallbackbox")
    al = load("unimatrix-autolink")
    real_ssh = al.ssh_identity_file
    real_pull = al.pull_membership
    monkeypatch.setattr(al, "pull_membership", lambda *a, **k: False)
    monkeypatch.setattr(al, "live_name", lambda *a, **k: None)
    monkeypatch.setattr(al, "ssh_config_aliases", lambda *a, **k: [])
    monkeypatch.setattr(al, "_known_hosts_entries", lambda *a, **k: [])
    monkeypatch.setattr(al, "ssh_identity_file", lambda roster, alias, run=None: None)
    monkeypatch.setattr(al, "pin_key_value", lambda roster, run=None: None)
    monkeypatch.setattr(al, "ssh_hostname", lambda roster, alias, run=None: None)
    roster = al._roster()
    live = set()
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(roster, "proc_start", lambda pid: f"start-{pid}")
    liveness = al._liveness()
    monkeypatch.setattr(liveness, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(liveness, "proc_start", lambda pid, platform=None: roster.proc_start(pid))

    class E:
        pass

    e = E()
    e.real_ssh_identity_file = real_ssh
    e.real_pull_membership = real_pull
    e.home, e.al, e.roster, e.live = home, al, roster, live
    e.sdir = home / "unimatrix"
    e.key = Path(roster.default_key())
    return e


def test_cli_entrypoint_unconfigured_is_silent_exit_zero_and_logs_reason(tmp_path):
    home = tmp_path / "s"
    home.mkdir()
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-autolink.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(home),
                            "UNIMATRIX_AUTOLINK_NO_ROUTER": "1"})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert "no supervisors started" in (home / "unimatrix" / "autolink.log").read_text()


# ---- membership snapshot as the member source ----------------------------------------------------

import base64  # noqa: E402
import shutil  # noqa: E402

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required")


def _keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text().strip()


class Snap:
    """A signed membership state builder over the real membership library and a real ed25519 signer key."""

    def __init__(self, env, tmp_path):
        self.m = env.al._membership()
        self.env = env
        self.signer, self.pub = _keygen(tmp_path / "signer")
        self.relay_pub = _keygen(tmp_path / "relay")[1]
        self.snap = None

    def member(self, box, user="kim"):
        return {"box": box, "host": f"{box}.local", "user": user, "relay_pub": self.relay_pub, "from_cidrs": []}

    def sign(self, change, key=None):
        snap = self.m.next_snapshot(self.snap, change)
        sig = self.m.FakeSigner(key or self.signer, environ={}).sign(self.m.canonical_bytes(snap))
        return snap, sig

    def adopt(self, *boxes):
        if self.m.load_pin(self.env.home) is None:
            self.m.write_pin(self.env.home, self.pub)
        for b in boxes:
            snap, sig = self.sign({"op": "join", "member": self.member(b)})
            self.m.adopt(self.env.home, snap, sig, notifier=lambda *a, **k: None)
            self.snap = snap


@pytest.fixture
def snap(env, tmp_path):
    return Snap(env, tmp_path)
