"""End to end: unimatrix-install and `unimatrix-autolink.py restart` replace a running peer router, faces return.

Real router and face processes run under a tmp settings home and a tmp HOME. The scripts are copied to a
tmp source tree whose roster namespaces every local endpoint by an env value, so the box's own router,
which owns the un-namespaced endpoints, is never reached.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
PEER = "agy-handoff-test"
WAIT = 45

_spec = importlib.util.spec_from_file_location("unimatrix_install_handoff", BIN / "unimatrix-install.py")
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

ENDPOINT_ANCHOR = 'hexname = hashlib.md5(f"unimatrix-{far_socket}".encode()).hexdigest()'
ENDPOINT_NAMESPACED = ('hexname = hashlib.md5(f"unimatrix-{os.environ.get(\'UNIMATRIX_TEST_NS\', \'\')}'
                       '{far_socket}".encode()).hexdigest()')
SHUTDOWN_ANCHOR = 'if op == "shutdown":'


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _swap(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"anchor {old!r} missing from {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _wait(what, probe, seconds=WAIT):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"{what} did not happen within {seconds}s")


class Box:
    """A tmp settings home with a patched script source tree, a live-router handle and its cleanup."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.sh = tmp_path / "settings"
        self.home = tmp_path / "home"
        self.dest = self.sh / "unimatrix"
        self.src = tmp_path / "src"
        self.sessions = self.home / ".claude" / "sessions"
        self.monkeypatch = monkeypatch
        self.seen_pids = set()
        for var, value in (("COORDINATOR_SETTINGS_HOME", self.sh), ("HOME", self.home), ("USERPROFILE", self.home),
                           ("CODEX_HOME", tmp_path / "codex"), ("GROK_HOME", tmp_path / "grok"),
                           ("AGY_BRAIN_DIR", tmp_path / "agy" / "brain"),
                           ("UNIMATRIX_TEST_NS", uuid.uuid4().hex)):
            monkeypatch.setenv(var, str(value))
        monkeypatch.delenv("UNIMATRIX_AUTOLINK_NO_ROUTER", raising=False)
        self.src.mkdir()
        names = set(ui.VENDORED_SCRIPTS) | set(ui.OPTIONAL_SCRIPTS) | {p.name for p in BIN.glob("unimatrix-*.py")}
        for name in sorted(names):
            (self.src / name).write_bytes((BIN / name).read_bytes())
        (self.src / "lib").mkdir()
        for name in ui.VENDORED_LIB:
            (self.src / "lib" / name).write_bytes((BIN / "lib" / name).read_bytes())
        _swap(self.src / "unimatrix-roster.py", ENDPOINT_ANCHOR, ENDPOINT_NAMESPACED)
        self.dest.mkdir(parents=True)
        (self.dest / "peers.json").write_text(
            json.dumps({"peers": [{"name": PEER, "vendor": "agy", "target": "conversation-handoff"}]}),
            encoding="utf-8")
        spec = importlib.util.spec_from_file_location("unimatrix_router_handoff_probe", self.src / "unimatrix-peer-router.py")
        self.router = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.router)
        self.roster = self.router._roster()
        legs = self.dest / "agy-legs"
        legs.mkdir()
        (legs / "conversation-handoff.json").write_text(
            json.dumps({"conversation": "conversation-handoff", "pid": os.getpid(),
                        "procStart": self.roster.proc_start(os.getpid())}), encoding="utf-8")

    def install(self):
        """Run the real `--install` at default arguments from the tmp source tree; (rc, output)."""
        buf = io.StringIO()
        self.monkeypatch.setattr(ui, "__file__", str(self.src / "unimatrix-install.py"))
        rc = ui.main(["--install"], version_text="2.1.316", dest_dir=self.dest, out=buf,
                     hooks_path=self.tmp / "gemini" / "hooks.json")
        return rc, buf.getvalue()

    def autolink(self, *args):
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, str(self.dest / "unimatrix-autolink.py"), *args], env=env,
                              capture_output=True, text=True, timeout=90, creationflags=_no_window())

    def list_reply(self):
        ctl = self.router.endpoint_path(self.router.ROUTER_ENDPOINT_SEED)
        try:
            reply = self.router.request_line(ctl, {"op": "list"}, timeout=5)
        except (OSError, ValueError):
            return None
        return reply if reply.get("ok") else None

    def router_pid(self):
        try:
            pid = json.loads((self.dest / "router" / "router.pid.json").read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError):
            return None
        if type(pid) is int:
            self.seen_pids.add(pid)
            return pid
        return None

    def faces(self):
        """pid -> registration of every live face registered under the peer's name."""
        out = {}
        for path in self.sessions.glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = entry.get("pid")
            if entry.get("name") == PEER and type(pid) is int and self.roster.pid_alive(pid):
                out[pid] = entry
                self.seen_pids.add(pid)
        return out

    def up(self, exclude_router=None, exclude_faces=()):
        """Wait for a router (not `exclude_router`) that answers `list` with a face (not in `exclude_faces`)."""
        def probe():
            pid = self.router_pid()
            if pid is None or pid == exclude_router or not self.list_reply():
                return None
            fresh = set(self.faces()) - set(exclude_faces)
            return (pid, fresh) if fresh else None
        return _wait("a router answering list with a registered face", probe)

    def gone(self, pid):
        _wait(f"pid {pid} to exit", lambda: not self.roster.pid_alive(pid), 20)

    def close(self):
        for pid in sorted(self.seen_pids | {self.router_pid() or 0}):
            if pid and self.roster.pid_alive(pid):
                self.roster.tree_kill(pid)
        rec = self.dest / "autolink-router.json"
        try:
            pid = json.loads(rec.read_text(encoding="utf-8")).get("pid")
            if type(pid) is int and self.roster.pid_alive(pid):
                self.roster.tree_kill(pid)
        except (OSError, ValueError):
            pass


@pytest.fixture(autouse=True)
def _install_isolation(monkeypatch, tmp_path):
    """No real engine root, doctrine, scheduler or registry is read or written by the install."""
    root = tmp_path / "engine"
    package = root / "coordinator_core" / "session"
    package.mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text("test-stamp", encoding="utf-8")
    (package / "external_registry.py").write_text("# capability fixture", encoding="utf-8")
    for sub, name in (("host_hooks", "session_context.py"), ("host_hooks", "codex_lifecycle.py"),
                      ("codex_status", "hook.py")):
        (package.parent / sub).mkdir(exist_ok=True)
        (package.parent / sub / name).write_text("# capability fixture", encoding="utf-8")
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (str(root), "published-target"))
    doc = tmp_path / "std-doctrine" / "global-doctrine"
    (doc / "rules").mkdir(parents=True)
    (doc / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    (doc / "rules" / "rules.md").write_text("- a rule\n", encoding="utf-8")
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    monkeypatch.setattr(ui, "run_schtasks", lambda *a: None)

    class Registry:
        HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

        def CreateKeyEx(self, *a):
            return self

        OpenKey = CreateKeyEx

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def SetValueEx(self, *a):
            pass

        def DeleteValue(self, *a):
            raise FileNotFoundError

    monkeypatch.setattr(ui, "_winreg", Registry)


@pytest.fixture
def box(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.importorskip("win32security")
    b = Box(tmp_path, monkeypatch)
    try:
        yield b
    finally:
        b.close()


def _installed_and_running(box):
    rc, out = box.install()
    assert rc == 0, out
    assert "router restarted" not in out  # no router was live: nothing to hand off
    result = box.autolink()
    assert result.returncode == 0, result.stderr
    return box.up()


def test_install_replaces_a_current_router_when_its_closure_changed(box):
    old_pid, old_faces = _installed_and_running(box)
    before = box.list_reply()
    assert before and "reachable" in before and PEER in before["peers"]

    rc, out = box.install()
    assert rc == 0 and "router restarted" not in out  # unchanged closure, router wrote router.pid.json
    assert box.router_pid() == old_pid
    assert box.roster.pid_alive(old_pid) and set(box.faces()) == set(old_faces)

    with open(box.src / "unimatrix-peer-router.py", "a", encoding="utf-8") as fh:
        fh.write("\n# upgrade marker\n")
    rc, out = box.install()
    assert rc == 0, out
    assert "router restarted: handshake" in out, out
    box.gone(old_pid)
    new_pid, new_faces = box.up(exclude_router=old_pid, exclude_faces=old_faces)
    assert new_pid != old_pid and box.router_pid() == new_pid
    for face in old_faces:
        box.gone(face)
    assert "# upgrade marker" in (box.dest / "unimatrix-peer-router.py").read_text(encoding="utf-8")
    after = box.list_reply()
    assert after and "reachable" in after and PEER in after["peers"]
    assert set(box.faces()) == set(new_faces)


def test_install_replaces_a_stale_router_without_the_shutdown_op_by_tree_kill(box):
    stale = box.src / "unimatrix-peer-router.py"
    pristine = stale.read_bytes()
    _swap(stale, SHUTDOWN_ANCHOR, 'if op == "shutdown-removed":')
    old_pid, old_faces = _installed_and_running(box)
    assert box.list_reply().get("ok")
    probe = box.router.request_line(box.router.endpoint_path(box.router.ROUTER_ENDPOINT_SEED), {"op": "shutdown"})
    assert probe == {"ok": False, "error": "unknown op"}
    os.remove(box.dest / "router" / "router.pid.json")  # a pre-handshake router never wrote it

    stale.write_bytes(pristine)
    rc, out = box.install()
    assert rc == 0, out
    assert "router restarted: tree_kill" in out, out
    box.gone(old_pid)
    for face in old_faces:
        box.gone(face)
    new_pid, new_faces = box.up(exclude_router=old_pid, exclude_faces=old_faces)
    assert new_pid != old_pid and box.router_pid() == new_pid
    reply = box.list_reply()
    assert reply and "reachable" in reply and PEER in reply["peers"]
    assert not (set(new_faces) & set(old_faces))


def test_autolink_restart_subcommand_hands_a_current_router_off_by_handshake(box):
    old_pid, old_faces = _installed_and_running(box)
    result = box.autolink("restart")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "router restarted: handshake" in result.stdout, result.stdout
    assert f"old pid {old_pid}" in result.stdout
    box.gone(old_pid)
    new_pid, new_faces = box.up(exclude_router=old_pid, exclude_faces=old_faces)
    assert new_pid != old_pid and box.router_pid() == new_pid
    for face in old_faces:
        box.gone(face)
    reply = box.list_reply()
    assert reply and "reachable" in reply and PEER in reply["peers"]
