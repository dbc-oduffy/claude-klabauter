"""Tests for unimatrix-install.py: version gate, install copy, hooks, peer doctrine, platform parity."""
import importlib.util
import io
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

def _can_mint_encrypted_key() -> bool:
    """True when the packages `unimatrix-passphrase.mint_key` serialises with import: a spec alone is not enough
    (a stray `bcrypt` directory has a spec and no `kdf`)."""
    try:
        from bcrypt import kdf  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: F401
    except ImportError:
        return False
    return True


needs_cryptography = pytest.mark.skipif(
    not _can_mint_encrypted_key(),
    reason="optional cryptography/bcrypt packages not importable; encrypted key minting needs both",
)

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-install.py"
_spec = importlib.util.spec_from_file_location("unimatrix_install", _SRC)
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

PLATFORMS = ["win32", "darwin", "linux"]
_REAL_RESOLVE = ui.resolve_engine_root
_REAL_HOST_PRESENT = ui.host_present
_REAL_PWSH_ALIAS_REMEDY = ui.pwsh_alias_remedy
_REAL_POWERSHELL_HOST = ui.powershell_host
_REAL_OPENSSH_REPORT = ui.openssh_receive_report
_REAL_IS_SCRATCH_ROOT = ui._seed().is_scratch_root


@pytest.fixture(autouse=True)
def _engine(monkeypatch, tmp_path):
    root = tmp_path / "engine"
    root.mkdir()
    package = root / "coordinator_core" / "session"
    package.mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text("test-stamp", encoding="utf-8")
    (package / "external_registry.py").write_text("# capability fixture", encoding="utf-8")
    (package.parent / "host_hooks").mkdir(exist_ok=True)
    (package.parent / "host_hooks" / "session_context.py").write_text("# capability fixture", encoding="utf-8")
    (package.parent / "host_hooks" / "codex_lifecycle.py").write_text("# capability fixture", encoding="utf-8")
    (package.parent / "codex_status").mkdir(exist_ok=True)
    (package.parent / "codex_status" / "hook.py").write_text("# capability fixture", encoding="utf-8")
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (str(root), "published-target"))
    return root


@pytest.fixture(autouse=True)
def _shared_item_box(monkeypatch):
    # The box's one passphrase item is titled `unimatrix-<local_box()>`; pin the box id so item titles are fixed.
    monkeypatch.setattr(ui._sibling("unimatrix-passphrase.py"), "_local_box", lambda: "me")


@pytest.fixture(autouse=True)
def _doctrine_beside_plugin(monkeypatch, tmp_path):
    # claude-klabauter carries no global-doctrine/ tree: a minimal one beside a stand-in plugin root keeps installs complete.
    doc = tmp_path / "std-doctrine" / "global-doctrine"
    (doc / "rules").mkdir(parents=True)
    (doc / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    (doc / "rules" / "rules.md").write_text("- a rule\n", encoding="utf-8")
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")


@pytest.fixture(autouse=True)
def _no_real_home_authorized_keys(monkeypatch, tmp_path):
    # --install rewrites ~/.ssh/authorized_keys by default: an unisolated test is redirected to a scratch file.
    real = (Path.home() / ".ssh" / "authorized_keys").resolve()
    scratch = tmp_path / "autouse-authorized_keys"
    original = ui.rewrite_forced_interpreters

    def guarded(path, interpreter):
        return original(scratch if Path(path).resolve() == real else path, interpreter)

    monkeypatch.setattr(ui, "rewrite_forced_interpreters", guarded)


@pytest.fixture(autouse=True)
def _no_real_scheduler(monkeypatch):
    monkeypatch.setattr(ui, "run_schtasks", lambda *a: None)
    fake = _FakeWinreg()
    monkeypatch.setattr(ui, "_winreg", lambda: fake)


@pytest.fixture(autouse=True)
def _hosts_present_and_no_real_powershell(monkeypatch):
    # Every host counts as installed and no PowerShell is spawned unless a test restores the real predicate.
    monkeypatch.setattr(ui, "host_present", lambda host: True)
    monkeypatch.setattr(ui, "pwsh_alias_remedy", lambda *a, **k: None)
    monkeypatch.setattr(ui, "powershell_host", lambda *a, **k: None)
    monkeypatch.setattr(ui, "openssh_receive_report", lambda *a, **k: 0)


@pytest.fixture(autouse=True)
def _pytest_tmp_roots_are_not_scratch(monkeypatch):
    # pytest's tmp_path sits under the system temp dir, which the seeder's scratch predicate refuses.
    monkeypatch.setattr(ui._seed(), "is_scratch_root", lambda path: False)


@pytest.fixture(autouse=True)
def _no_real_launchctl_ps_or_pin(monkeypatch):
    monkeypatch.setattr(ui, "run_launchctl", lambda *a: None)
    monkeypatch.setattr(ui._lib("unimatrix_relay_sessions"), "ps_snapshot", lambda *a, **k: "")
    monkeypatch.setattr(ui, "_pin_value", lambda: None)
    monkeypatch.setattr(ui, "_pin_raw", lambda: __file__)  # a live pin file: record_python_pin writes nothing
    monkeypatch.setattr(ui, "_write_pin", lambda interp: pytest.fail("machine-local write outside a pin test"))


_REAL_LINK_GROK = ui.link_grok_local_bin


@pytest.fixture(autouse=True)
def _no_real_grok_link(monkeypatch):
    monkeypatch.setattr(ui, "link_grok_local_bin", lambda *a, **k: "skipped")


_REAL_MAIN = ui.main


@pytest.fixture(autouse=True)
def _full_install_unless_plain(monkeypatch, request):
    """Legacy tests exercise the full host and autolink install: a bare `--install` gets every host and --autolink
    spelled out. A test named `test_plain_install_*` drives the real defaults."""
    if request.node.name.startswith("test_plain_install_"):
        return

    def full(argv=None, *a, **k):
        argv = list(argv or [])
        if "--install" in argv:
            if "--hosts" not in argv:
                argv += ["--hosts", "codex,agy,grok"]
            if "--autolink" not in argv:
                argv.append("--autolink")
        return _REAL_MAIN(argv, *a, **k)

    monkeypatch.setattr(ui, "main", full)


def _write_stubs(src, router_text="# stub PipeServer\n"):
    for n in ui.VENDORED_SCRIPTS:
        (src / n).write_text(router_text if n == ui.ROUTER_SCRIPT else "# stub\n", encoding="utf-8")


def _stub_src(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_stubs(src)
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))
    # A hermetic skills source: the repo's own coordinator/skills need not exist (it is DoE's tree).
    # Trap: resolve_skills_root searches `<SKILLS_ROOT.parent>/skills`, so the dir must be named `skills`.
    skills = tmp_path / "skills"
    (skills / "required").mkdir(parents=True)
    (skills / "required" / "SKILL.md").write_text("# required\n", encoding="utf-8")
    (skills / "codex-core-skills.txt").write_text("required\n", encoding="utf-8")
    monkeypatch.setattr(ui, "SKILLS_ROOT", skills)


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(ui, "trust_codex_hook", lambda p: (True, "x"))
    return home


# Split so the publish rename (authoring key -> published key) cannot collapse the two
# registry keys into one: a repeated keyword is a SyntaxError in the published mirror.
_AUTHORING_KEY = "project_" + "claude-klabauter"


class _FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """In-memory winreg: HKCU Run values only."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, fail=False):
        self.values, self.fail, self.opened = {}, fail, []

    def CreateKeyEx(self, root, sub, reserved, access):
        if self.fail:
            raise PermissionError("denied")
        self.opened.append((root, sub))
        return _FakeKey(self.values)

    def OpenKey(self, root, sub, reserved, access):
        self.opened.append((root, sub))
        return _FakeKey(self.values)

    def SetValueEx(self, key, name, reserved, kind, data):
        assert kind == self.REG_SZ
        key.values[name] = data

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]


class _Launchctl:
    """A fake launchctl: `print` succeeds for a loaded label, `bootout` unloads it."""

    def __init__(self, loaded):
        self.loaded, self.calls = set(loaded), []

    def __call__(self, *args):
        self.calls.append(args)
        label = args[1].rsplit("/", 1)[-1]
        rc = 0 if label in self.loaded else 113
        if args[0] == "bootout":
            self.loaded.discard(label)
        return subprocess.CompletedProcess(["launchctl", *args], rc, "", "")


LEGACY = "com.coordinator.unimatrix-autolink"


def _plist(agents, label):
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{label}.plist").write_bytes(b"<plist/>")


def test_legacy_login_agent_is_booted_out_and_unlinked_then_a_rerun_is_a_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    agents = tmp_path / "LaunchAgents"
    _plist(agents, LEGACY)
    _plist(agents, ui.LOGIN_AGENT_LABEL)
    lc = _Launchctl({LEGACY})
    assert ui.remove_legacy_login_agents(agents, lc, uid=501) == [LEGACY]
    assert ("bootout", f"gui/501/{LEGACY}") in lc.calls
    assert [p.name for p in agents.iterdir()] == [f"{ui.LOGIN_AGENT_LABEL}.plist"]
    calls_before = len(lc.calls)
    assert ui.remove_legacy_login_agents(agents, lc, uid=501) == []
    assert not any(c[0] == "bootout" for c in lc.calls[calls_before:])


def test_legacy_label_loaded_without_a_plist_is_still_booted_out(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    lc = _Launchctl({LEGACY})
    assert ui.remove_legacy_login_agents(tmp_path / "LaunchAgents", lc, uid=7) == [LEGACY]
    assert ("bootout", f"gui/7/{LEGACY}") in lc.calls


def test_legacy_removal_touches_nothing_off_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    agents = tmp_path / "LaunchAgents"
    _plist(agents, LEGACY)
    lc = _Launchctl({LEGACY})
    assert ui.remove_legacy_login_agents(agents, lc) == [] and lc.calls == []
    assert (agents / f"{LEGACY}.plist").exists()


def test_main_install_leaves_one_autolink_label_and_a_rerun_removes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    hooks = tmp_path / "g" / "hooks.json"
    agents = tmp_path / "g" / "login-agent"
    _plist(agents, LEGACY)
    lc = _Launchctl({LEGACY})
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf, hooks_path=hooks,
                 launchctl=lc, relay_ps=lambda: "", relay_kill=lambda pid: None)
    assert rc == 0 and f"legacy login agent removed: {LEGACY}" in buf.getvalue()
    assert [p.name for p in agents.iterdir()] == [f"{ui.LOGIN_AGENT_LABEL}.plist"]
    buf = io.StringIO()
    ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf, hooks_path=hooks,
            launchctl=lc, relay_ps=lambda: "", relay_kill=lambda pid: None)
    assert "legacy login agent removed" not in buf.getvalue()
    assert [p.name for p in agents.iterdir()] == [f"{ui.LOGIN_AGENT_LABEL}.plist"]


def _lstart(epoch):
    t = __import__("time").localtime(epoch)
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"Mon {months[t.tm_mon - 1]} {t.tm_mday:2d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} {t.tm_year}"


def _ps_row(pid, ppid, epoch, dest, script="unimatrix-relay.py"):
    return f"{pid} {ppid} {_lstart(epoch)} /usr/bin/python3 {dest / script}"


def test_main_install_reaps_with_the_injected_ps_and_kill(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    dest = tmp_path / "d"
    monkeypatch.setattr(ui, "_pid_alive", lambda pid: False)
    killed = []
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=dest, out=buf, hooks_path=tmp_path / "g" / "hooks.json",
                 launchctl=_Launchctl(()), relay_ps=lambda: _ps_row(401, 1, 1_000_000_000, dest), relay_kill=killed.append)
    assert rc == 0 and killed == [401] and "relay sessions reaped: 1 orphaned, 0 on replaced code" in buf.getvalue()


class _PairBox:
    """Fake subprocess.run for --pair: ssh -G, _machine_local get and set, ssh-keygen, and the 1Password `op` CLI."""

    def __init__(self, store=None, op_missing=False, items=None, vault="Vault"):
        self.store, self.calls = dict(store or {}), []
        self.op_missing, self.items, self.vault = op_missing, dict(items or {}), vault

    def _op(self, argv, done):
        if self.op_missing:
            raise FileNotFoundError("op")
        if argv[1] == "read":
            name = argv[-1].split("/")[3]
            return done(0, self.items[name]) if name in self.items else done(1, "", "isn't an item in the vault")
        if argv[1:3] == ["item", "create"]:
            title = next(a for a in argv if a.startswith("--title="))[len("--title="):]
            self.items[title] = "generated-pass-" + title
            return done()
        raise AssertionError(argv)

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        done = lambda rc=0, out="", err="": type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()
        if argv[0] == "op":
            return self._op(argv, done)
        if argv[0] == "ssh":
            return done(0, "")
        if argv[0] == "ssh-keygen" and argv[1] == "-lf":
            return done(0, "256 SHA256:FAKEFP unimatrix (ED25519)\n")
        if argv[0] == "ssh-keygen":
            path = Path(argv[argv.index("-f") + 1])
            path.write_text("PRIVATE", encoding="utf-8")
            Path(str(path) + ".pub").write_text("ssh-ed25519 AAAAPAIRBODY unimatrix\n", encoding="utf-8")
            return done()
        if argv[2] == "get" and argv[3] == "unimatrix.passphrase_vault":
            return done(0, self.vault) if self.vault else done(1)
        if argv[2] == "get":
            return done(0, self.store[argv[3]]) if argv[3] in self.store else done(1)
        if argv[2] == "dump":
            keep = {k: v for k, v in self.store.items() if k.startswith(argv[4] + ".")}
            return done(0, json.dumps(keep))
        self.store[argv[3]] = argv[4]
        return done()


def _pair_home(monkeypatch, tmp_path, reader=True):
    home = tmp_path / "sh"
    (home / "bin").mkdir(parents=True)
    if reader:
        (home / "bin" / "_machine_local.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def test_pair_mints_records_and_second_run_reads_same_key(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    refs = []
    priv, body = ui.pair_key("far-1", run=box, unencrypted=True, on_mint=refs.append)
    assert priv == home / ".secrets" / "ssh" / "unimatrix-far-1" and body == "AAAAPAIRBODY"
    assert box.store == {"unimatrix.keys.far-1": str(priv)} and refs == []
    keygens = [c for c in box.calls if c[0] == "ssh-keygen"]
    assert len(keygens) == 1 and "-N" in keygens[0] and keygens[0][keygens[0].index("-N") + 1] == ""
    assert not [c for c in box.calls if c[0] == "op"]
    priv2, body2 = ui.pair_key("far-1", run=box, unencrypted=True)
    assert (priv2, body2) == (priv, body) and len([c for c in box.calls if c[0] == "ssh-keygen"]) == 1


def _passphrase():
    return ui._sibling("unimatrix-passphrase.py")


@needs_cryptography
def test_pair_default_mints_a_passphrase_encrypted_key_from_the_1password_item(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    refs = []
    priv, body = ui.pair_key("far-1", run=box, on_mint=refs.append)
    assert priv == home / ".secrets" / "ssh" / "unimatrix-far-1" and _passphrase().is_encrypted(priv)
    assert refs == ["op://Vault/unimatrix-me/password"] and box.items == {"unimatrix-me": "generated-pass-unimatrix-me"}
    assert box.store == {"unimatrix.keys.far-1": str(priv)}
    assert not [c for c in box.calls if c[0] == "ssh-keygen"]
    secret = box.items["unimatrix-me"]
    assert not [c for c in box.calls if any(secret in a for a in c)]
    assert not [p for p in home.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    first = priv.read_bytes()
    priv2, body2 = ui.pair_key("far-1", run=box)
    assert (priv2, body2) == (priv, body) and priv.read_bytes() == first


def test_pair_default_with_no_op_cli_refuses_naming_the_passphrase_source(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    with pytest.raises(_passphrase().PassphraseSourceMissing, match="passphrase_source_missing: op CLI"):
        ui.pair_key("far-1", run=_PairBox(op_missing=True))
    assert not (home / ".secrets" / "ssh" / "unimatrix-far-1").exists()
    buf = io.StringIO()
    assert ui.main(["--pair", "far-1"], out=buf, pair_run=_PairBox(op_missing=True)) == 6
    assert "REFUSE: passphrase_source_missing: op CLI" in buf.getvalue()


def test_pair_default_with_unset_vault_key_refuses_naming_the_key(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    buf = io.StringIO()
    assert ui.main(["--pair", "far-1"], out=buf, pair_run=_PairBox(vault="")) == 6
    assert "REFUSE: passphrase_source_missing" in buf.getvalue() and "unimatrix.passphrase_vault" in buf.getvalue()


def test_pair_default_with_locked_1password_refuses_typed(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)

    class Locked(_PairBox):
        def _op(self, argv, done):
            return done(1, "", "You are not currently signed in")

    buf = io.StringIO()
    assert ui.main(["--pair", "far-1"], out=buf, pair_run=Locked()) == 6
    assert "REFUSE: 1password_locked" in buf.getvalue()


def _remint_box(home, monkeypatch):
    box = _PairBox(items={"unimatrix-me": "old-secret"})
    key = home / ".secrets" / "ssh" / "unimatrix-far-1"
    key.parent.mkdir(parents=True)
    key.write_text("OLD", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 OLDBODY c\n", encoding="utf-8")
    box.store["unimatrix.keys.far-1"] = str(key)
    return box, key


@needs_cryptography
def test_pair_remint_replaces_the_pair_under_the_same_shared_item(monkeypatch, tmp_path):
    box, key = _remint_box(_pair_home(monkeypatch, tmp_path), monkeypatch)
    refs = []
    priv, body = ui.pair_key("far-1", run=box, remint=True, on_mint=refs.append)
    assert priv == key and body != "OLDBODY" and _passphrase().is_encrypted(key)
    assert box.items == {"unimatrix-me": "old-secret"}
    assert refs == ["op://Vault/unimatrix-me/password"]
    assert not list(key.parent.glob("*.new*"))
    assert not [c for c in box.calls if c[0] == "op" and c[1] == "item"]


@needs_cryptography
def test_pair_remint_mints_the_new_key_under_the_existing_passphrase(monkeypatch, tmp_path):
    box, key = _remint_box(_pair_home(monkeypatch, tmp_path), monkeypatch)
    mod = _passphrase()
    seen = []
    real_mint = mod.mint_key
    monkeypatch.setattr(mod, "mint_key", lambda p, c, pw: (seen.append((Path(p).name, pw, key.read_text(encoding="utf-8"))),
                                                          real_mint(p, c, pw))[1])
    ui.pair_key("far-1", run=box, remint=True)
    assert seen == [("unimatrix-far-1.new", "old-secret", "OLD")]


@needs_cryptography
def test_two_link_keys_on_one_box_share_one_item_ref_and_create_no_per_alias_item(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    refs = []
    ui.pair_key("far-1", run=box, on_mint=refs.append)
    ui.pair_key("far-2", run=box, on_mint=refs.append)
    assert refs == ["op://Vault/unimatrix-me/password"] * 2
    assert list(box.items) == ["unimatrix-me"]
    assert len([c for c in box.calls if c[:3] == ["op", "item", "create"]]) == 1


def test_pair_remint_failing_before_the_new_key_is_written_keeps_the_superseded_key_and_item(monkeypatch, tmp_path):
    box, key = _remint_box(_pair_home(monkeypatch, tmp_path), monkeypatch)
    mod = _passphrase()

    def boom(p, c, pw):
        Path(p).write_text("PARTIAL", encoding="utf-8")
        raise RuntimeError("mint failed")

    monkeypatch.setattr(mod, "mint_key", boom)
    with pytest.raises(RuntimeError, match="mint failed"):
        ui.pair_key("far-1", run=box, remint=True)
    assert key.read_text(encoding="utf-8") == "OLD" and Path(str(key) + ".pub").is_file()
    assert box.items == {"unimatrix-me": "old-secret"} and not list(key.parent.glob("*.new*"))


def test_pair_remint_with_locked_1password_keeps_the_superseded_key_and_item(monkeypatch, tmp_path):
    box, key = _remint_box(_pair_home(monkeypatch, tmp_path), monkeypatch)
    mod = _passphrase()

    def locked(alias, run=None):
        raise mod.OnePasswordLocked("1password_locked: test")

    monkeypatch.setattr(mod, "ensure_item", locked)
    with pytest.raises(mod.OnePasswordLocked):
        ui.pair_key("far-1", run=box, remint=True)
    assert key.read_text(encoding="utf-8") == "OLD" and Path(str(key) + ".pub").is_file()
    assert box.items == {"unimatrix-me": "old-secret"}


@needs_cryptography
def test_pair_remint_replaces_the_public_half_before_the_private_half(monkeypatch, tmp_path):
    box, key = _remint_box(_pair_home(monkeypatch, tmp_path), monkeypatch)
    order = []
    real = ui.os.replace
    monkeypatch.setattr(ui.os, "replace", lambda a, b: (order.append(Path(b).name), real(a, b))[1])
    ui.pair_key("far-1", run=box, remint=True)
    assert order == ["unimatrix-far-1.pub", "unimatrix-far-1"]


def test_pair_default_without_cryptography_refuses_typed_and_leaves_no_key(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    mod = _passphrase()

    def missing(p, c, pw):
        raise ImportError("No module named 'cryptography'")

    monkeypatch.setattr(mod, "mint_key", missing)
    buf = io.StringIO()
    assert ui.main(["--pair", "far-1"], out=buf, pair_run=_PairBox()) != 0
    assert "cryptography" in buf.getvalue()
    assert not (home / ".secrets" / "ssh" / "unimatrix-far-1").exists()


@needs_cryptography
def test_file_key_default_is_encrypted_and_remint_with_no_op_refuses(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    buf = io.StringIO()
    box = _PairBox()
    assert ui.main(["--file-key"], out=buf, pair_run=box) == 0
    assert "passphrase: op://Vault/unimatrix-me/password" in buf.getvalue()
    assert _passphrase().is_encrypted(home / ".secrets" / "ssh" / "unimatrix-file")
    keygens = [c for c in box.calls if c[0] == "ssh-keygen"]
    assert len(keygens) == 1 and keygens[0][1] == "-lf"
    buf = io.StringIO()
    assert ui.main(["--file-key", "--remint"], out=buf, pair_run=_PairBox(op_missing=True)) == 6
    assert "REFUSE: passphrase_source_missing" in buf.getvalue()
    assert (home / ".secrets" / "ssh" / "unimatrix-file").is_file()


def test_pair_adopts_unrecorded_existing_key_without_overwriting(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    key = home / ".secrets" / "ssh" / "unimatrix-x"
    key.parent.mkdir(parents=True)
    key.write_text("MINE", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 EXISTINGBODY c\n", encoding="utf-8")
    box = _PairBox()
    priv, body = ui.pair_key("x", run=box)
    assert body == "EXISTINGBODY" and key.read_text(encoding="utf-8") == "MINE"
    assert not [c for c in box.calls if c[0] == "ssh-keygen"] and box.store["unimatrix.keys.x"] == str(key)


def _recorded_key(home, box, alias, body="RECORDEDBODY"):
    key = home / ".secrets" / "ssh" / f"unimatrix-{alias}"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIV", encoding="utf-8")
    Path(str(key) + ".pub").write_text(f"ssh-ed25519 {body} c\n", encoding="utf-8")
    box.store[f"unimatrix.keys.{alias}"] = str(key)
    return key


def test_pair_key_adopt_only_returns_the_recorded_key_and_never_mints_or_records(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    key = _recorded_key(home, box, "far-1")
    monkeypatch.setattr(ui, "_mint", lambda *a, **k: pytest.fail("adopt_only reached _mint"))
    assert ui.pair_key("far-1", run=box, adopt_only=True) == (key, "RECORDEDBODY")
    assert not [c for c in box.calls if c[0] in ("op", "ssh-keygen") or c[2:3] == ["set"]]


def test_pair_key_adopt_only_without_a_record_raises_naming_the_alias_and_changes_nothing(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    monkeypatch.setattr(ui, "_mint", lambda *a, **k: pytest.fail("adopt_only reached _mint"))
    with pytest.raises(RuntimeError, match="no recorded pair key for far-1"):
        ui.pair_key("far-1", run=box, adopt_only=True)
    assert box.store == {} and not [c for c in box.calls if c[0] in ("op", "ssh-keygen") or c[2:3] == ["set"]]
    assert not (home / ".secrets").exists()


def test_pair_key_adopt_only_refuses_a_record_whose_file_is_missing(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox(store={"unimatrix.keys.far-1": str(home / "gone")})
    with pytest.raises(RuntimeError, match="far-1"):
        ui.pair_key("far-1", run=box, adopt_only=True)
    assert "unimatrix.keys.far-1" in box.store and not [c for c in box.calls if c[2:3] == ["set"]]


def test_pair_key_adopt_only_with_remint_is_refused(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        ui.pair_key("far-1", run=_PairBox(), adopt_only=True, remint=True)


class _IdentityBox(_PairBox):
    """_PairBox whose `ssh -G <alias>` reports the IdentityFile mapped to that alias (the deployed per-pair key)."""

    def __init__(self, identities=None, **kw):
        super().__init__(**kw)
        self.identities = dict(identities or {})

    def __call__(self, argv, **kw):
        if argv[0] == "ssh" and argv[1] == "-G":
            self.calls.append(list(argv))
            path = self.identities.get(argv[-1])
            out = f"hostname {argv[-1]}\nidentityfile {path}\n" if path else "hostname h\n"
            return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        return super().__call__(argv, **kw)


def _ssh_key(home, name, body):
    key = home / "dotssh" / name
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIV", encoding="utf-8")
    Path(str(key) + ".pub").write_text(f"ssh-ed25519 {body} c\n", encoding="utf-8")
    return key


def test_recorded_pair_keys_lists_each_registry_peer_by_record_else_ssh_identity(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    a = _ssh_key(home, "zero-to-far-1", "BODYONE")
    b = _ssh_key(home, "zero-to-far-2", "BODYTWO")
    box = _IdentityBox({"far-1": a})
    box.store["unimatrix.far_boxes"] = "far-1, far-2 far-3"
    box.store["unimatrix.keys.far-2"] = str(b)
    monkeypatch.setattr(ui, "_mint", lambda *a, **k: pytest.fail("recorded_pair_keys reached _mint"))
    assert ui.recorded_pair_keys(run=box) == {"far-1": (a, "BODYONE"), "far-2": (b, "BODYTWO")}
    assert not [c for c in box.calls if c[0] in ("op", "ssh-keygen") or c[2:3] == ["set"]]


def test_recorded_pair_keys_ignores_a_stray_record_outside_the_registry(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    real = _ssh_key(home, "zero-to-far-1", "REALBODY")
    stray = _recorded_key(home, _PairBox(), "x", "STRAYBODY")
    box = _IdentityBox({"far-1": real}, store={"unimatrix.keys.x": str(stray), "unimatrix.far_boxes": "far-1"})
    assert ui.recorded_pair_keys(run=box) == {"far-1": (real, "REALBODY")}


def test_recorded_pair_keys_lists_requested_peers_with_no_registry_and_ignores_a_stray(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    real = _ssh_key(home, "zero-macmini-to-machine-b", "REALBODY")
    stray = _recorded_key(home, _PairBox(), "x", "STRAYBODY")
    box = _IdentityBox({"machine-b": real}, store={"unimatrix.keys.x": str(stray)})
    assert ui.recorded_pair_keys(run=box, peers={"machine-b", "macmini"}) == {"machine-b": (real, "REALBODY")}
    assert not [c for c in box.calls if c[0] in ("op", "ssh-keygen") or c[2:3] == ["set"]]


def test_recorded_pair_keys_refuses_a_requested_peer_whose_identity_has_no_pub_and_never_mints(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    key = _ssh_key(home, "zero-to-far-1", "BODY")
    Path(str(key) + ".pub").unlink()
    box = _IdentityBox({"far-1": key}, store={"unimatrix.far_boxes": "far-1"})
    monkeypatch.setattr(ui, "_mint", lambda *a, **k: pytest.fail("recorded_pair_keys reached _mint"))
    assert ui.recorded_pair_keys(run=box) == {}
    with pytest.raises(RuntimeError, match="pair key for far-1"):
        ui.recorded_pair_keys(run=box, peers={"far-1"})
    assert not [c for c in box.calls if c[0] in ("op", "ssh-keygen") or c[2:3] == ["set"]]


def test_recorded_pair_keys_skips_a_peer_whose_identity_is_missing_or_default(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _IdentityBox({"far-1": home / "dotssh" / "gone"}, store={"unimatrix.far_boxes": "far-1 far-2"})
    assert ui.recorded_pair_keys(run=box) == {}


def test_recorded_pair_keys_empty_when_no_registry_and_refuses_without_reader(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    assert ui.recorded_pair_keys(run=_PairBox()) == {}
    _pair_home(monkeypatch, tmp_path / "other", reader=False)
    with pytest.raises(RuntimeError, match="_machine_local.py"):
        ui.recorded_pair_keys(run=_PairBox())


@needs_cryptography
def test_pair_sanitizes_alias_and_refuses_empty(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    priv, _ = ui.pair_key("a b/c", run=_PairBox())
    assert priv.name == "unimatrix-a-b-c" and priv.parent == home / ".secrets" / "ssh"
    with pytest.raises(RuntimeError):
        ui.pair_key("///", run=_PairBox())


def test_pair_without_machine_local_reader_fails_before_minting(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path, reader=False)
    box = _PairBox()
    with pytest.raises(RuntimeError):
        ui.pair_key("x", run=box)
    assert not [c for c in box.calls if c[0] == "ssh-keygen"]


def test_pair_main_prints_line_and_block_and_exits_zero(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--pair", "x", "--unencrypted-key"], out=buf, pair_run=_PairBox())
    text = buf.getvalue()
    assert rc == 0 and "Host x\n" in text and "IdentityFile " in text and "unimatrix-x" in text
    assert any(l.startswith('restrict,command="') and l.endswith("ssh-ed25519 AAAAPAIRBODY unimatrix")
               and 'unimatrix-relay.py"' in l for l in text.splitlines())


@needs_cryptography
def test_pair_with_signer_pin_emits_request_flow_and_no_hand_edit(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    pin = home / "unimatrix" / "membership" / "signer.pub"
    mem = ui._sibling("unimatrix-membership.py")
    pin.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mem, "load_pin", lambda h: "ssh-ed25519 AAAA")
    box = _FarBox(tmp_path / "far")
    buf = io.StringIO()
    assert ui.main(["--pair", "x"], out=buf, pair_run=box) == 0
    text = buf.getvalue()
    assert "unimatrix-pair.py request --box x" in text and "Host x" not in text
    assert "authorized_keys line" not in text and "ssh-ed25519 AAAAPAIRBODY" not in text
    assert not (tmp_path / "far" / ".ssh").exists()


def test_pair_main_refuses_with_nonzero_when_key_cannot_be_recorded(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path, reader=False)
    buf = io.StringIO()
    assert ui.main(["--pair", "x"], out=buf, pair_run=_PairBox()) == 6 and "REFUSE" in buf.getvalue()


class _FarBox(_PairBox):
    """_PairBox whose ssh runs the stdin program locally under a fake HOME, as the far box would; the platform
    the probe reports is the simulated far box's (`far_platform`, a posix box by default), never the host's."""

    far_platform = "linux"

    def __init__(self, home, reply=None, **kw):
        super().__init__(**kw)
        self.home, self.reply, self.sent = home, reply, []

    def __call__(self, argv, **kw):
        if argv[0] != "ssh" or "-G" in argv:
            return super().__call__(argv, **kw)
        self.calls.append(list(argv))
        done = lambda rc, out: type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
        if self.reply is not None:
            return done(0, self.reply)
        if argv[argv.index("--") + 2] == "python3" and getattr(self, "no_python3", False):
            return done(127, "")
        self.sent.append(kw["input"])
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
               "COORDINATOR_SETTINGS_HOME": str(self.home / "fsh")}
        program = kw["input"]
        # Trap: keyed on the installer program text; if it stops naming IsUserAnAdmin the stub
        # no-ops and win32 results follow the host's real admin state again.
        if "IsUserAnAdmin" in program:
            program = ("import sys\nif sys.platform == 'win32':\n    import ctypes\n"
                       "    ctypes.windll.shell32.IsUserAnAdmin = lambda: 0\n" + program)
        r = subprocess.run([sys.executable, "-"], input=program, capture_output=True, text=True, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = r.stdout
        if "import os, sys" in kw["input"] and "\t" in out:
            out = self.far_platform + out[out.index("\t"):]
        return done(r.returncode, out)


def test_pair_installs_line_once_over_authorized_link(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    far = tmp_path / "far"
    far.mkdir()
    box = _FarBox(far)
    status, line = ui.install_over_link("x", "AAAAPAIRBODY", run=box)
    assert status == "added"
    keys = far / ".ssh" / "authorized_keys"
    assert keys.read_text(encoding="utf-8") == line + "\n" and f"{sys.executable} " in line
    assert str(far / "fsh" / "unimatrix") in line
    if sys.platform != "win32":
        assert keys.stat().st_mode & 0o777 == 0o600 and keys.parent.stat().st_mode & 0o777 == 0o700
    assert ui.install_over_link("x", "AAAAPAIRBODY", run=box)[0] == "present"
    assert keys.read_text(encoding="utf-8") == line + "\n"


def test_pair_install_appends_after_unterminated_existing_line(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    keys = tmp_path / "far" / ".ssh" / "authorized_keys"
    keys.parent.mkdir(parents=True)
    keys.write_text("ssh-ed25519 OTHER me", encoding="utf-8")
    assert ui.install_over_link("x", "NEWBODY", run=_FarBox(tmp_path / "far"))[0] == "added"
    assert keys.read_text(encoding="utf-8").splitlines()[0] == "ssh-ed25519 OTHER me"
    assert len(keys.read_text(encoding="utf-8").splitlines()) == 2


def test_pair_install_tries_python_when_python3_is_absent(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    box = _FarBox(tmp_path / "far")
    box.no_python3 = True
    assert ui.install_over_link("x", "B", run=box)[0] == "added"
    assert [c[c.index("--") + 2] for c in box.calls if c[0] == "ssh"][:2] == ["python3", "python"]
    assert all("BatchMode=yes" in c for c in box.calls if c[0] == "ssh")


@needs_cryptography
def test_pair_dead_link_sends_nothing_and_prints_paste_instruction(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    for reply in ("", "relay says hello\n", "linux\t/usr/bin/python3\n"):
        box = _FarBox(tmp_path / "far", reply=reply)
        assert ui.install_over_link("x", "B", run=box) == ("no-link", None)
        assert not (tmp_path / "far" / ".ssh").exists()
    box = _FarBox(tmp_path / "far", reply="")
    buf = io.StringIO()
    assert ui.main(["--pair", "x"], out=buf, pair_run=box) == 0
    assert "no authorized link to x: paste the line above on the far box" in buf.getvalue()


@needs_cryptography
def test_pair_main_reports_added_then_present(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    box = _FarBox(tmp_path / "far")
    first, second = io.StringIO(), io.StringIO()
    assert ui.main(["--pair", "x"], out=first, pair_run=box) == 0
    assert ui.main(["--pair", "x"], out=second, pair_run=box) == 0
    assert "added on x" in first.getvalue() and "already present on x" in second.getvalue()
    assert len((tmp_path / "far" / ".ssh" / "authorized_keys").read_text(encoding="utf-8").splitlines()) == 1


@needs_cryptography
def test_pair_windows_admin_far_box_is_not_written(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    seen = []

    def run(argv, **kw):
        if argv[0] != "ssh" or "-G" in argv:
            return box(argv, **kw)
        done = lambda out: type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()
        if "input" in kw and "IsUserAnAdmin" in kw["input"]:
            seen.append(kw["input"])
            return done("admin\n")
        bs = chr(92)
        win_home = bs.join(["C:", "Users", "me", ".coordinator-claude-settings"])
        return done(f"win32\tC:{bs}Python312{bs}python.exe\t{win_home}\n")

    status, line = ui.install_over_link("x", "B", run=run)
    assert status == "admin" and seen and "C:\\Python312\\python.exe" in line  # abs-path-ok: synthetic Windows interpreter path fixture
    buf = io.StringIO()
    assert ui.main(["--pair", "x"], out=buf, pair_run=run) == 0 and "administrators_authorized_keys" in buf.getvalue()


LAUNCHER_CPU_BUDGET_S = 0.5
LAUNCHER_HANG_GUARD_S = 60


@pytest.mark.spawns_process
def test_lifecycle_launcher_never_imports_ambient_module_for_stale_engine(tmp_path):
    engine = tmp_path / "stale engine"
    (engine / "coordinator_core").mkdir(parents=True)
    (engine / "coordinator_core" / "_engine_stamp").write_text("stamped", encoding="utf-8")
    ambient = tmp_path / "ambient"
    package = ambient / "coordinator_core" / "session"
    package.mkdir(parents=True)
    for directory in (package, package.parent):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    (package / "external_registry.py").write_text("raise RuntimeError('AMBIENT MODULE LOADED')", encoding="utf-8")
    script = Path(ui.__file__).with_name(ui.EXTERNAL_SESSION_SCRIPT)
    proc = subprocess.run([sys.executable, str(script), "--engine-root", str(engine), "--event", "SessionEnd"],
                          input="{}", text=True, capture_output=True, timeout=3,
                          env=dict(os.environ, PYTHONPATH=str(ambient)),
                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert proc.returncode == 0
    assert "degraded" in json.loads(proc.stdout)["systemMessage"]
    assert "external session context unavailable" in proc.stderr
    assert "AMBIENT MODULE LOADED" not in proc.stderr


def _fake_engine(tmp_path):
    pkg = tmp_path / "eng" / "coordinator_core" / "tui_status"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text("import sys\nprint('host=' + ' '.join(sys.argv[1:]))\n")
    return tmp_path / "eng"


@pytest.mark.skipif(os.name != "nt", reason="runs the Windows PowerShell script")
def test_tui_status_grok_ps1_renders_from_a_foreign_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "_console_python", lambda: sys.executable)
    root = _fake_engine(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    shutil.copy(ui._BIN_DIR / ui.TUI_STATUS_LAUNCHER, dest / ui.TUI_STATUS_LAUNCHER)
    cfg = tmp_path / "grokhome" / "config.toml"
    assert ui.seed_host_status_line(cfg, "grok", engine_root=str(root), dest_dir=dest) == "created"
    cmd = tomllib.loads(cfg.read_text())["ui"]["status_line"]["command"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-File", cmd], input="{}", cwd=elsewhere, env=env,
                          capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "host=grok"


# `"toolPermission": "always-proceed"` in agy's settings.json approves every tool call regardless of --mode.
_BYPASS = {"toolPermission": "always-proceed"}


def test_control_key_mints_beside_ssh_and_prints_alias_block_and_control_line(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    fake_home = tmp_path / "userhome"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    buf = io.StringIO()
    box = _PairBox()
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8"], out=buf, pair_run=box) == 0
    text = buf.getvalue()
    # Trap: the publish scrub maps a box name to a placeholder differently inside an identifier-shaped
    # token than as a bare word, so the key file name is built from the bare name, never spelled whole.
    key = fake_home / ".ssh" / ("unimatrix_control_" + "machine-a")
    assert key.is_file() and f"control key for machine-a: {key}" in text
    assert "Host unimatrix-control-machine-a\n" in text and "    HostName machine-a.local\n" in text
    assert 'from="10.0.0.0/8" ssh-ed25519 AAAAPAIRBODY unimatrix-control' in text.splitlines()
    assert "restrict" not in text and "command=" not in text
    assert not [c for c in box.calls if c[0] != "ssh" and "unimatrix.keys" in " ".join(c)]


class _AliasBox(_PairBox):
    def __call__(self, argv, **kw):
        if argv[:2] == ["ssh", "-G"]:
            self.calls.append(list(argv))
            return type("R", (), {"returncode": 0, "stderr": "", "stdout": self.ssh_g})()
        return super().__call__(argv, **kw)


def test_control_key_default_mints_unencrypted_and_never_touches_the_passphrase_vault(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    fake_home = tmp_path / "userhome"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    buf = io.StringIO()
    box = _PairBox(op_missing=True)
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8"], out=buf, pair_run=box) == 0
    kg = [c for c in box.calls if c[0] == "ssh-keygen" and "-f" in c]
    assert kg and kg[0][kg[0].index("-N") + 1] == ""
    assert not [c for c in box.calls if c[0] == "op"]
    assert "passphrase" not in buf.getvalue() and "REFUSE" not in buf.getvalue()


def test_control_key_no_link_prints_the_pinned_line_and_writes_no_record(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    buf = io.StringIO()
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8"], out=buf, pair_run=_PairBox()) == 0
    text = buf.getvalue()
    line = next(l for l in text.splitlines() if l.startswith('from="'))
    assert line == 'from="10.0.0.0/8" ssh-ed25519 AAAAPAIRBODY unimatrix-control'
    assert "record is absent" in text and "<far-" not in text and "python3" not in text
    assert not (tmp_path / "sh" / "unimatrix" / "control-far").exists()


def test_control_key_block_carries_the_peer_hostkeyalias_and_user(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    monkeypatch.setattr(ui.getpass, "getuser", lambda: "localuser")
    buf = io.StringIO()
    box = _AliasBox()
    box.ssh_g = "host machine-a\nuser machine_auser\nhostkeyalias machine-a.local\nport 22\n"
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8"], out=buf, pair_run=box) == 0
    text = buf.getvalue()
    assert "    HostKeyAlias machine-a.local\n" in text and "    User machine_auser\n" in text
    assert ["ssh", "-G", "--", "machine-a"] in box.calls


def test_control_key_block_omits_absent_peer_options_never_inventing_them(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    monkeypatch.setattr(ui.getpass, "getuser", lambda: "localuser")
    buf = io.StringIO()
    box = _AliasBox()
    box.ssh_g = "host machine-a\nuser localuser\nport 22\n"
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8"], out=buf, pair_run=box) == 0
    assert "HostKeyAlias" not in buf.getvalue() and "    User " not in buf.getvalue()


def test_control_install_over_link_writes_the_control_line(monkeypatch, tmp_path):
    far = tmp_path / "far"
    far.mkdir()
    status, line = ui.install_over_link("x", "AAAAPAIRBODY", run=_FarBox(far), control=True, from_cidrs=["10.0.0.0/8"])
    assert status == "added" and line == 'from="10.0.0.0/8" ssh-ed25519 AAAAPAIRBODY unimatrix-control'
    assert (far / ".ssh" / "authorized_keys").read_text(encoding="utf-8") == line + "\n"


def _record(home, box):
    return json.loads((home / "unimatrix" / "control-far" / f"{box}.json").read_text(encoding="utf-8"))


def test_control_install_over_link_records_the_probed_far_invocation(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    far = tmp_path / "far"
    far.mkdir()
    assert ui.install_over_link("machine-a", "B", run=_FarBox(far), control=True, from_cidrs=["10.0.0.0/8"])[0] == "added"
    rec = _record(home, "machine-a")
    assert rec == {"platform": "linux", "interpreter": sys.executable,
                   "script": posixpath.join(str(far / "fsh"), "unimatrix", "unimatrix-control.py"), "shell": "posix"}
    assert not list((home / "unimatrix" / "control-far").glob("*.tmp"))


@pytest.mark.parametrize("status_seen", ["present", "admin"])
def test_control_record_is_written_on_present_and_admin_links_too(monkeypatch, tmp_path, status_seen):
    home = _pair_home(monkeypatch, tmp_path)
    far = tmp_path / "far"
    far.mkdir()
    box = _FarBox(far)
    if status_seen == "present":
        ui.install_over_link("machine-a", "B", run=box, control=True, from_cidrs=["10.0.0.0/8"])
        (home / "unimatrix" / "control-far" / "machine-a.json").unlink()
    else:
        monkeypatch.setattr(ui, "_APPEND_PROGRAM", "print('admin')\n")
    status, _ = ui.install_over_link("machine-a", "B", run=box, control=True, from_cidrs=["10.0.0.0/8"])
    assert status == status_seen and _record(home, "machine-a")["shell"] == "posix"


class _WinFar:
    """A far Windows box: answers the probe triple, then the DefaultShell registry read."""

    def __init__(self, default_shell):
        self.default_shell, self.calls = default_shell, []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw.get("input")))
        out = ("win32\tC:\\Py\\python.exe\tC:\\Users\\me\\.coordinator-claude-settings\n"
               if "import os, sys" in kw["input"] else self.default_shell + "\n" if "winreg" in kw["input"] else "added\n")
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()


@pytest.mark.parametrize("default_shell,shell", [
    ("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "powershell"),
    ("C:\\Program Files\\PowerShell\\7\\pwsh.exe", "powershell"),
    ("C:\\Windows\\System32\\cmd.exe", "cmd"), ("", "cmd")])
def test_control_record_for_a_windows_far_box_reads_the_openssh_default_shell(monkeypatch, tmp_path, default_shell, shell):
    home = _pair_home(monkeypatch, tmp_path)
    box = _WinFar(default_shell)
    ui.install_over_link("machine-a", "B", run=box, control=True, from_cidrs=["10.0.0.0/8"])
    assert _record(home, "machine-a") == {
        "platform": "win32", "interpreter": "C:\\Py\\python.exe",
        "script": "C:\\Users\\me\\.coordinator-claude-settings\\unimatrix\\unimatrix-control.py", "shell": shell}
    assert sum(1 for _, i in box.calls if "winreg" in i) == 1


def test_control_record_reads_no_registry_on_a_posix_far_box(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    far = tmp_path / "far"
    far.mkdir()
    box = _FarBox(far)
    ui.install_over_link("machine-a", "B", run=box, control=True, from_cidrs=["10.0.0.0/8"])
    assert not [i for i in box.sent if "winreg" in i]


def test_control_key_writes_the_record_over_a_link_and_prints_the_line(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    far = tmp_path / "far"
    far.mkdir()
    buf = io.StringIO()
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8", "--from-cidr", "192.168.1.0/24"],
                   out=buf, pair_run=_FarBox(far)) == 0
    line = 'from="10.0.0.0/8,192.168.1.0/24" ssh-ed25519 AAAAPAIRBODY unimatrix-control'
    assert line in buf.getvalue().splitlines()
    assert (far / ".ssh" / "authorized_keys").read_text(encoding="utf-8") == line + "\n"
    assert _record(home, "machine-a")["interpreter"] == sys.executable


def test_control_key_without_a_cidr_source_refuses_naming_the_flag(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    buf = io.StringIO()
    box = _PairBox()
    assert ui.main(["--control-key", "machine-a"], out=buf, pair_run=box) == 6
    assert "REFUSE" in buf.getvalue() and "--from-cidr" in buf.getvalue()
    assert not (tmp_path / "userhome" / ".ssh").exists() and not (home / "unimatrix").exists()


def test_control_key_refuses_a_value_that_is_not_a_cidr(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    buf = io.StringIO()
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "lan"], out=buf, pair_run=_PairBox()) == 6
    assert "REFUSE" in buf.getvalue() and "not a CIDR" in buf.getvalue()


def test_control_key_takes_the_pin_from_this_boxs_verified_membership(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "userhome"))
    members = [{"box": "other", "from_cidrs": ["1.1.1.0/24"]}, {"box": "me", "from_cidrs": ["10.0.0.0/8", "fd00::/8"]}]
    membership = ui._sibling("unimatrix-membership.py")
    monkeypatch.setattr(membership, "verified_members", lambda home, run=None: members)
    monkeypatch.setattr(ui._sibling("unimatrix-peer-contract.py"), "local_box", lambda: "me")
    buf = io.StringIO()
    assert ui.main(["--control-key", "machine-a"], out=buf, pair_run=_PairBox()) == 0
    assert 'from="10.0.0.0/8,fd00::/8" ssh-ed25519 AAAAPAIRBODY unimatrix-control' in buf.getvalue().splitlines()
    buf = io.StringIO()
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "172.16.0.0/12"], out=buf, pair_run=_PairBox()) == 0
    assert 'from="172.16.0.0/12" ssh-ed25519' in buf.getvalue()


def test_control_key_never_overwrites_and_refuses_an_empty_box(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    fake_home = tmp_path / "userhome"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    key = fake_home / ".ssh" / "unimatrix_control_x"
    key.parent.mkdir(parents=True)
    key.write_text("MINE", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 EXISTINGBODY c\n", encoding="utf-8")
    box = _PairBox()
    assert ui.control_key("x", run=box) == (key, "EXISTINGBODY")
    assert key.read_text(encoding="utf-8") == "MINE" and not [c for c in box.calls if c[0] == "ssh-keygen"]
    with pytest.raises(RuntimeError):
        ui.control_key("///", run=box)


_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabcdefgh unimatrix"


_SSH_OK = {"installed": True, "running": True, "automatic": True, "firewall22": True, "keys_exists": True,
           "keys_protected": True, "keys_sids": ["S-1-5-18", "S-1-5-32-544"]}


class _PsRun:
    """Fake PowerShell: answers the state script from `states` in order and the apply script with `apply_rc`."""

    def __init__(self, states, apply_rc=0):
        self.states, self.apply_rc, self.argv = list(states), apply_rc, []

    def __call__(self, argv, **kw):
        self.argv.append(argv)
        assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if "NEEDS_ADMIN" in argv[-1]:
            return subprocess.CompletedProcess(argv, self.apply_rc, "NEEDS_ADMIN" if self.apply_rc == 3 else "OK", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.states.pop(0)) + "\n", "")


def test_openssh_report_complete_incomplete_and_needs_admin(monkeypatch):
    monkeypatch.setattr(ui, "openssh_receive_report", _REAL_OPENSSH_REPORT)
    host = "pwsh-stub"
    out = io.StringIO()
    assert ui.openssh_receive_report(out, _PsRun([_SSH_OK]), host=host) == 0
    assert "complete" in out.getvalue()
    out = io.StringIO()
    run = _PsRun([{}])
    assert ui.openssh_receive_report(out, run, host=host) == 1
    assert "openssh-firewall-22" in out.getvalue() and "--openssh-receive" in out.getvalue()
    assert len(run.argv) == 1 and run.argv[0][0] == host
    out = io.StringIO()
    assert ui.openssh_receive_report(out, _PsRun([{}], apply_rc=3), apply=True, host=host) == 1
    assert "elevated" in out.getvalue()


def test_openssh_apply_runs_the_script_then_rechecks(monkeypatch):
    monkeypatch.setattr(ui, "openssh_receive_report", _REAL_OPENSSH_REPORT)
    out, run = io.StringIO(), _PsRun([{}, _SSH_OK])
    assert ui.openssh_receive_report(out, run, apply=True, host="pwsh-stub") == 0
    scripts = [a[-1] for a in run.argv]
    assert "Set-Service" in scripts[1] and "S-1-5-32-544" in scripts[1] and "icacls" in scripts[1]
    assert len(scripts) == 3


def test_main_openssh_receive_flag_drives_the_apply_and_is_windows_only(monkeypatch):
    monkeypatch.setattr(ui, "openssh_receive_report", _REAL_OPENSSH_REPORT)
    monkeypatch.setattr(ui, "powershell_host", lambda *a, **k: "pwsh-stub")
    monkeypatch.setattr(ui.sys, "platform", "linux")
    buf = io.StringIO()
    assert ui.main(["--openssh-receive"], out=buf, openssh_run=lambda *a, **k: pytest.fail("ran on linux")) == 0
    assert "Windows only" in buf.getvalue()
    monkeypatch.setattr(ui.sys, "platform", "win32")
    buf, run = io.StringIO(), _PsRun([{}, _SSH_OK])
    assert ui.main(["--openssh-receive"], out=buf, openssh_run=run) == 0
    assert "complete" in buf.getvalue()


def test_install_reports_the_openssh_receive_side_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "openssh_receive_report", _REAL_OPENSSH_REPORT)
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "powershell_host", lambda *a, **k: "pwsh-stub")
    monkeypatch.setattr(ui.sys, "platform", "win32")
    buf = io.StringIO()
    ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf, hooks_path=tmp_path / "h.json",
            openssh_run=_PsRun([{}]))
    assert "openssh receive side incomplete" in buf.getvalue()




posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")


_OLD = (3, 9, 6)  # a fake pre-3.11 sys.version_info for the gate


_REAL_NAMES = json.loads((Path(__file__).parent / "fixtures" / "host-real-tool-names.json").read_text(encoding="utf-8"))
