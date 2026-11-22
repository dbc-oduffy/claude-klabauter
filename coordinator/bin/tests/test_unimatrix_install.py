"""Tests for unimatrix-install.py: version gate, install copy, hooks, peer doctrine, platform parity."""
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

needs_cryptography = pytest.mark.skipif(
    importlib.util.find_spec("cryptography") is None or importlib.util.find_spec("bcrypt") is None,
    reason="optional cryptography/bcrypt packages absent; encrypted key minting needs both",
)

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-install.py"
_spec = importlib.util.spec_from_file_location("unimatrix_install", _SRC)
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

PLATFORMS = ["win32", "darwin", "linux"]
_REAL_RENDER_PEER = ui.render_peer_doctrine
_REAL_RESOLVE = ui.resolve_engine_root
_REAL_HOST_PRESENT = ui.host_present
_REAL_SEED_STANDING_DOCTRINE = ui._seed_standing_doctrine
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
def _doctrine_beside_plugin(monkeypatch, tmp_path):
    # claude-klabauter carries no global-doctrine/ tree: a minimal one beside a stand-in plugin root keeps installs complete.
    doc = tmp_path / "std-doctrine" / "global-doctrine"
    (doc / "rules").mkdir(parents=True)
    (doc / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    (doc / "rules" / "rules.md").write_text("- a rule\n", encoding="utf-8")
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    skills = tmp_path / "std-doctrine" / "skills"
    (skills / "required").mkdir(parents=True)
    (skills / "required" / "SKILL.md").write_text("# required\n", encoding="utf-8")
    monkeypatch.setattr(ui, "SKILLS_ROOT", skills)


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


def _run(argv, text, dest=None, hooks=None):
    buf = io.StringIO()
    rc = ui.main(argv, version_text=text, dest_dir=dest, out=buf, hooks_path=hooks)
    return rc, buf.getvalue()


def _write_stubs(src, router_text="# stub PipeServer\n"):
    for n in ui.VENDORED_SCRIPTS:
        (src / n).write_text(router_text if n == ui.ROUTER_SCRIPT else "# stub\n", encoding="utf-8")


def _stub_src(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_stubs(src)
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))


def test_range_constant_and_scripts():
    assert ui.VERIFIED_RANGE == ((2, 1, 300), (2, 2, 0))
    assert ui.VENDORED_SCRIPTS == (
        "unimatrix-bridge.py",
        "unimatrix-relay.py",
        "unimatrix-rekey.py",
        "unimatrix-inbox.py",
        "unimatrix-roster.py",
        "unimatrix-peer-contract.py",
        "unimatrix-local-transport.py",
        "unimatrix-peer-codex.py",
        "unimatrix-codex-launch.py",
        "unimatrix-peer-agy.py",
        "unimatrix-peer-grok.py",
        "unimatrix-agy-leg.py",
        "unimatrix-peer-router.py",
        "unimatrix-agy-hook-pretool.py",
        "unimatrix-agy-hook-launch.py",
        "unimatrix-agy-hook-sessionstart.py",
        "unimatrix-codex-hook-sessionstart.py",
        "unimatrix-host-guard.py",
        "coordinator-external-session.py",
        "unimatrix-doctrine-seed.py",
        "unimatrix-autolink.py",
        "unimatrix-liveness.py",
        "unimatrix-mirror-all.py",
        "unimatrix-reshape.py",
        "unimatrix-membership.py",
        "unimatrix-doctor.py",
        "unimatrix-pair.py",
        "unimatrix-file.py",
        "unimatrix-file-relay.py",
        "unimatrix-passphrase.py",
        "unimatrix-control.py",
        "unimatrix-tui-status-launch.py",
        "headless-em-launch.py",
    )
    assert "session_detach.py" in ui.VENDORED_LIB


def test_publish_list_names_the_membership_scripts():
    targets = ui._BIN_DIR.parents[1] / "setup" / "publish-targets.portable"
    if not targets.exists():
        pytest.skip(f"{targets} absent: publish targets are not part of this tree")
    line = next((l for l in targets.read_text(encoding="utf-8").splitlines()
                 if l.startswith("claude-klabauter-coordinator-bin|")), None)
    if line is None:
        pytest.skip("publish-targets.portable has no claude-klabauter-coordinator-bin row")
    names = set(line.split("|")[6].split(","))
    assert {"unimatrix-membership.py", "unimatrix-doctor.py", "unimatrix-pair.py"} <= names


def test_vendored_scripts_cover_real_autolink_closure():
    closure = ui.script_closure(ui._BIN_DIR)
    assert ui.AUTOLINK_SCRIPT in closure
    assert {"unimatrix-roster.py", "unimatrix-mirror-all.py", "unimatrix-reshape.py"} <= closure
    assert closure <= set(ui.VENDORED_SCRIPTS) | set(ui.OPTIONAL_SCRIPTS)


def test_install_seeds_closure_script_outside_vendored_list(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    src = tmp_path / "src"
    (src / "unimatrix-autolink.py").write_text("# runs unimatrix-newpiece.py\n", encoding="utf-8")
    assert _run(["--install"], "2.1.316", tmp_path / "d")[0] != 0
    (src / "unimatrix-newpiece.py").write_text("# new\n", encoding="utf-8")
    assert _run(["--install"], "2.1.316", tmp_path / "d")[0] == 0
    assert (tmp_path / "d" / "unimatrix-newpiece.py").is_file()


def test_parse_version():
    assert ui.parse_version("2.1.316 (Claude Code)") == (2, 1, 316)
    with pytest.raises(RuntimeError, match="no semver in `claude --version` output"):
        ui.parse_version("garbage")


@pytest.mark.parametrize(
    "text,status",
    [
        ("2.1.300 (Claude Code)", "ok"),
        ("2.1.316 (Claude Code)", "ok"),
        ("2.1.999 (Claude Code)", "ok"),
        ("2.1.299 (Claude Code)", "refuse"),
        ("2.2.0 (Claude Code)", "refuse"),
        ("3.0.0 (Claude Code)", "refuse"),
        ("1.9.9 (Claude Code)", "refuse"),
    ],
)
def test_check_version_boundaries(text, status):
    assert ui.check_version(text, False)[0] == status


def test_force_turns_refusal_into_warn():
    status, msg = ui.check_version("2.2.0", True)
    assert status == "warn" and msg.startswith("WARN")
    assert ui.check_version("2.1.316", True)[0] == "ok"


@pytest.mark.parametrize("plat", PLATFORMS)
def test_main_check_mode_per_platform(monkeypatch, plat):
    monkeypatch.setattr(sys, "platform", plat)
    rc, out = _run([], "2.1.316 (Claude Code)")
    assert rc == 0 and "in verified range" in out
    rc, out = _run([], "2.2.0 (Claude Code)")
    assert rc != 0 and "REFUSE" in out
    rc, out = _run(["--force"], "2.2.0 (Claude Code)")
    assert rc == 0 and "WARN" in out


def test_unparseable_version_refuses():
    rc, out = _run([], "nonsense")
    assert rc != 0 and "no semver" in out


@pytest.mark.parametrize("plat", PLATFORMS)
def test_install_copies_scripts_and_prints_rc_notice(monkeypatch, tmp_path, plat):
    monkeypatch.setattr(sys, "platform", plat)
    src = tmp_path / "src"
    src.mkdir()
    _write_stubs(src)
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))
    dest = tmp_path / "home" / "unimatrix"
    rc, out = _run(["--install"], "2.1.316", dest, tmp_path / "gemini" / "hooks.json")
    assert rc == 0
    assert "/rc" in out and out.index("/rc") < out.index("installed")
    assert sorted(p.name for p in dest.iterdir()) == sorted(ui.VENDORED_SCRIPTS)


def test_install_prints_authorized_keys_line_with_absolute_paths(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    rc, out = _run(["--install"], "2.1.316", dest, tmp_path / "gemini" / "hooks.json")
    assert rc == 0
    line = [l for l in out.splitlines() if "restrict,command=" in l][0]
    assert f"{ui.forced_command_interpreter()} {dest / 'unimatrix-relay.py'}\" ssh-ed25519" in line
    assert Path(ui.forced_command_interpreter()).is_absolute()


def test_authorized_keys_line_windows_form(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    line = ui.authorized_keys_line(r"\Users\me\unimatrix", interpreter=r"\Python312\python.exe")
    assert line.startswith(r'restrict,command="\Python312\python.exe \Users\me\unimatrix')
    assert line.endswith(r'unimatrix-relay.py" ssh-ed25519 <key-body> unimatrix')


def test_authorized_keys_line_escapes_quotes_for_paths_with_spaces():
    line = ui.authorized_keys_line("/home/a b/unimatrix", interpreter="/usr/bin/python3")  # abs-path-ok: synthetic unimatrix path with a space, never opened
    assert line.startswith('restrict,command="/usr/bin/python3 \\"')
    assert 'unimatrix-relay.py\\"" ssh-ed25519' in line


def test_macos_prefers_homebrew_python_over_stub(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(tmp_path / "absent"), str(brew)))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "name", "posix")
    assert ui.forced_command_interpreter() == str(brew)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(tmp_path / "absent"),))
    assert ui.forced_command_interpreter() == sys.executable
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(brew),))
    monkeypatch.setattr(sys, "platform", "linux")
    assert ui.forced_command_interpreter() == sys.executable


def test_install_refuses_outside_range_without_copying(monkeypatch, tmp_path):
    dest = tmp_path / "unimatrix"
    rc, out = _run(["--install"], "2.2.0", dest)
    assert rc != 0 and not dest.exists()
    assert "/rc" not in out


def test_install_force_outside_range_proceeds(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_stubs(src)
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))
    dest = tmp_path / "unimatrix"
    rc, out = _run(["--install", "--force"], "2.2.0", dest, tmp_path / "h" / "hooks.json")
    assert rc == 0 and "WARN" in out and dest.is_dir()


def _pipeless_src(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_stubs(src, router_text="# stub without the transport\n")
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))
    return src


def test_win32_pipeless_source_router_refused_nothing_copied(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    _pipeless_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    rc, out = _run(["--install"], "2.1.316", dest, tmp_path / "h" / "hooks.json")
    assert rc != 0 and not dest.exists()
    assert out.count("REFUSE") == 1 and "named-pipe" in out and "klabauter published mirror" in out
    assert "installed" not in out


def test_win32_downgrade_of_installed_pipe_router_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    _pipeless_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    dest.mkdir(parents=True)
    installed = dest / ui.ROUTER_SCRIPT
    installed.write_text("# has PipeServer\n", encoding="utf-8")
    rc, out = _run(["--install"], "2.1.316", dest, tmp_path / "h" / "hooks.json")
    assert rc != 0 and "downgrade" in out and "named-pipe" in out
    assert [p.name for p in dest.iterdir()] == [ui.ROUTER_SCRIPT]
    assert installed.read_text(encoding="utf-8") == "# has PipeServer\n"


def test_win32_pipe_capable_source_copies(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    rc, _ = _run(["--install"], "2.1.316", dest, tmp_path / "h" / "hooks.json")
    assert rc == 0 and (dest / ui.ROUTER_SCRIPT).is_file()


@pytest.mark.parametrize("plat", ["darwin", "linux"])
def test_posix_pipeless_source_router_copies(monkeypatch, tmp_path, plat):
    monkeypatch.setattr(sys, "platform", plat)
    _pipeless_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    rc, _ = _run(["--install"], "2.1.316", dest, tmp_path / "h" / "hooks.json")
    assert rc == 0 and (dest / ui.ROUTER_SCRIPT).is_file()


def test_install_missing_sources_fails(monkeypatch, tmp_path):
    src = tmp_path / "empty"
    src.mkdir()
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))
    rc, out = _run(["--install"], "2.1.316", tmp_path / "d", tmp_path / "h" / "hooks.json")
    assert rc != 0 and "missing" in out
    assert not (tmp_path / "h").exists()


def _hook_cmd(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))[ui.AGY_HOOK_KEY]["PreToolUse"][0]["hooks"][0]["command"]


def test_install_seeds_hooks_json_when_absent(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    hp = tmp_path / "gemini" / "config" / "hooks.json"
    rc, out = _run(["--install"], "2.1.316", dest, hp)
    assert rc == 0 and "created" in out
    entry = json.loads(hp.read_text(encoding="utf-8"))[ui.AGY_HOOK_KEY]["PreToolUse"][0]
    assert entry["matcher"] == ui.AGY_TOOL_MATCHER
    cmd = _hook_cmd(hp)
    assert _fwd(sys.executable) in cmd and _fwd(dest / ui.AGY_HOOK_SCRIPT) in _shown(cmd)
    assert "coordinator/bin" not in cmd.replace("\\", "/")


def test_install_leaves_existing_key_byte_identical(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    hp = tmp_path / "hooks.json"
    raw = '{"x":1,  "unimatrix-agy-leg": {"custom": true}}'
    hp.write_text(raw, encoding="utf-8")
    rc, out = _run(["--install"], "2.1.316", tmp_path / "d", hp)
    assert rc == 0 and "present" in out
    assert hp.read_text(encoding="utf-8") == raw


def test_install_adds_only_our_key_to_existing(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    hp = tmp_path / "hooks.json"
    other = {"theirs": {"PreToolUse": [{"matcher": "a", "hooks": []}]}, "n": [1, 2, {"k": None}]}
    hp.write_text(json.dumps(other), encoding="utf-8")
    rc, out = _run(["--install"], "2.1.316", tmp_path / "d", hp)
    assert rc == 0 and "added" in out
    data = json.loads(hp.read_text(encoding="utf-8"))
    assert ui.AGY_HOOK_KEY in data
    del data[ui.AGY_HOOK_KEY]
    assert data == other


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]"])
def test_install_refuses_unreadable_hooks_json_untouched(monkeypatch, tmp_path, raw):
    _stub_src(monkeypatch, tmp_path)
    hp = tmp_path / "hooks.json"
    hp.write_text(raw, encoding="utf-8")
    rc, out = _run(["--install"], "2.1.316", tmp_path / "d", hp)
    assert rc != 0 and "REFUSE" in out
    assert hp.read_text(encoding="utf-8") == raw


def test_uninstall_removes_only_our_key(tmp_path):
    hp = tmp_path / "hooks.json"
    other = {"theirs": {"a": 1}}
    hp.write_text(json.dumps({**other, ui.AGY_HOOK_KEY: ui.agy_hook_entry(tmp_path)}), encoding="utf-8")
    rc, out = _run(["--uninstall"], None, None, hp)
    assert rc == 0 and "removed" in out
    assert json.loads(hp.read_text(encoding="utf-8")) == other
    assert (tmp_path).is_dir()


def test_uninstall_without_our_key_or_file_changes_nothing(tmp_path):
    hp = tmp_path / "hooks.json"
    rc, out = _run(["--uninstall"], None, None, hp)
    assert rc == 0 and "absent" in out and not hp.exists()
    raw = '{"theirs":   1}'
    hp.write_text(raw, encoding="utf-8")
    rc, out = _run(["--uninstall"], None, None, hp)
    assert rc == 0 and hp.read_text(encoding="utf-8") == raw


def test_uninstall_refuses_unparseable_untouched(tmp_path):
    hp = tmp_path / "hooks.json"
    hp.write_text("{bad", encoding="utf-8")
    rc, _ = _run(["--uninstall"], None, None, hp)
    assert rc != 0 and hp.read_text(encoding="utf-8") == "{bad"


def test_settings_home_derives_from_home(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setattr(ui.Path, "home", classmethod(lambda cls: tmp_path))
    assert ui.settings_home() == tmp_path / ".coordinator-claude-settings"


@pytest.mark.parametrize("plat,expect_flag", [("win32", True), ("darwin", False), ("linux", False)])
def test_probe_uses_which_and_windows_creationflags(monkeypatch, plat, expect_flag):
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(ui.shutil, "which", lambda name: "/x/claude.cmd")
    seen = {}

    class R:
        stdout = "2.1.316 (Claude Code)\n"

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return R()

    monkeypatch.setattr(ui.subprocess, "run", fake_run)
    assert ui.probe_claude_version().startswith("2.1.316")
    assert seen["cmd"] == ["/x/claude.cmd", "--version"]
    assert ("creationflags" in seen["kw"]) is expect_flag


def test_probe_missing_claude(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="claude not found"):
        ui.probe_claude_version()


@pytest.mark.parametrize("plat,name", [("linux", "claude"), ("darwin", "claude"), ("win32", "claude.exe")])
def test_probe_finds_claude_in_local_bin_when_path_lacks_it(monkeypatch, tmp_path, plat, name):
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(sys, "platform", plat)
    monkeypatch.setattr(ui.shutil, "which", lambda n: None)
    exe = home / ".local" / "bin" / name
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    seen = {}

    class R:
        stdout = "2.1.316 (Claude Code)\n"

    monkeypatch.setattr(ui.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and R())
    assert ui.probe_claude_version().startswith("2.1.316")
    assert seen["cmd"] == [str(exe), "--version"]


def test_path_hit_wins_over_local_bin(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "claude").write_text("x")
    monkeypatch.setattr(ui.shutil, "which", lambda n: "/on/path/claude")
    assert ui.resolve_claude() == "/on/path/claude"


def test_script_name_constants_are_pinned():
    assert ui.AGY_SESSIONSTART_SCRIPT == "unimatrix-agy-hook-sessionstart.py"
    assert ui.CODEX_SESSIONSTART_SCRIPT == "unimatrix-codex-hook-sessionstart.py"
    assert ui.AGY_SESSIONSTART_SCRIPT in ui.VENDORED_SCRIPTS
    assert ui.CODEX_SESSIONSTART_SCRIPT in ui.VENDORED_SCRIPTS


def test_agy_entry_carries_pretool_and_sessionstart(tmp_path):
    entry = ui.agy_hook_entry(tmp_path)
    assert entry["PreToolUse"][0]["matcher"] == ui.AGY_TOOL_MATCHER
    handler = entry["SessionStart"][0]
    # agy's SessionStart is flat: a grouped entry makes agy reject the whole hooks.json.
    assert handler["type"] == "command" and "hooks" not in handler and "matcher" not in handler
    assert _fwd(tmp_path / ui.AGY_SESSIONSTART_SCRIPT) in _shown(handler["command"])
    assert _fwd(sys.executable) in handler["command"]


def test_agy_entry_carries_a_flat_preinvocation_handler_running_the_pretool_script(tmp_path):
    handler = ui.agy_hook_entry(tmp_path)["PreInvocation"][0]
    assert handler["type"] == "command" and "hooks" not in handler and "matcher" not in handler
    assert _fwd(tmp_path / ui.AGY_HOOK_SCRIPT) in _shown(handler["command"])
    assert "--event PreInvocation" in _shown(handler["command"])


def test_agy_seed_upgrades_entry_lacking_sessionstart(tmp_path):
    hp = tmp_path / "hooks.json"
    old = ui.agy_hook_entry(tmp_path)
    del old["SessionStart"]
    hp.write_text(json.dumps({"x": 1, ui.AGY_HOOK_KEY: old}), encoding="utf-8")
    assert ui.seed_agy_hook(hp, tmp_path) == "updated"
    data = json.loads(hp.read_text(encoding="utf-8"))
    assert data["x"] == 1 and data[ui.AGY_HOOK_KEY] == ui.agy_hook_entry(tmp_path)
    assert ui.seed_agy_hook(hp, tmp_path) == "present"


def test_codex_hooks_path_honours_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ch"))
    assert ui.codex_hooks_path() == tmp_path / "ch" / "hooks.json"
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setattr(ui.Path, "home", classmethod(lambda cls: tmp_path))
    assert ui.codex_hooks_path() == tmp_path / ".codex" / "hooks.json"


@pytest.fixture()
def runtime_box(tmp_path, monkeypatch):
    from coordinator_core.install import host_install_codex
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(ui, "external_session_capability", lambda root: (True, "ready"))
    monkeypatch.setattr(ui, "install_scripts", lambda *a: [])
    monkeypatch.setattr(ui, "_seed_standing_doctrine", lambda *a: [])
    monkeypatch.setattr(ui, "render_peer_doctrine", lambda *a: f"{ui.DOCTRINE_BEGIN}\npeer doctrine\n{ui.DOCTRINE_END}\n")
    monkeypatch.setattr(ui, "trust_codex_hook", lambda hp, **kw: (True, "verified"))
    return tmp_path / "engine", ui.codex_hooks_path()


@pytest.mark.parametrize("consent", [False, None, "true", 1])
def test_runtime_enrollment_refuses_without_literal_consent(runtime_box, monkeypatch, consent):
    root, hp = runtime_box
    monkeypatch.setattr(ui, "host_present", lambda *a: pytest.fail("must validate before work"))
    assert ui.enroll_codex_runtime(root, consent=consent) == 2
    assert not hp.parent.exists()


def test_strict_runtime_renders_selected_peer_template(runtime_box, monkeypatch):
    root, hooks = runtime_box
    plugin = root / "selected plugin"
    template = plugin / "templates" / "unimatrix" / "peer-doctrine.md"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setattr(ui, "render_peer_doctrine", _REAL_RENDER_PEER)
    for check in (True, False):
        assert ui.enroll_codex_runtime(root, consent=True, check=check) == 7
        assert not hooks.exists()

    template.parent.mkdir(parents=True)
    template.write_text("Selected sizing instructions. Send {SEND}; invoke {INVOKE}.")
    rules = ui.codex_rules_path()
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("Operator paragraph.\n")
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    expected = _REAL_RENDER_PEER(ui.settings_home() / "unimatrix", template)
    assert "Operator paragraph." in rules.read_text()
    assert expected.strip() in rules.read_text()
    assert "{SEND}" not in expected and "{INVOKE}" not in expected
    before = rules.read_bytes()
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    assert rules.read_bytes() == before


def test_strict_runtime_renders_packaged_peer_template(runtime_box, monkeypatch):
    root, hooks = runtime_box
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(ui, "render_peer_doctrine", _REAL_RENDER_PEER)
    rules = ui.codex_rules_path()
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text("Operator paragraph.\n")
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    expected = _REAL_RENDER_PEER(ui.settings_home() / "unimatrix")
    assert expected.index("at most 2 files and 30 lines") < expected.index("coordinator-invoke engine_ask.submit")
    assert "check a large valid range with a small result" in expected
    assert "headless-em-launch codex --cwd <repo> -- exec -s workspace-write" in expected
    assert "reconcile it with the engine records" in expected
    assert "returned no ID, report the denied path" in expected
    assert "On timeout, keep watching that fire" in expected
    assert "verify the product at `commit_sha`" in expected
    assert "Operator paragraph." in rules.read_text()
    assert expected.strip() in rules.read_text()
    assert "{SEND}" not in expected and "{INVOKE}" not in expected
    before = rules.read_bytes()
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    assert rules.read_bytes() == before


def test_runtime_enrollment_rerun_preserves_foreign_handlers(runtime_box):
    root, hp = runtime_box
    hp.parent.mkdir()
    foreign = {"hooks": [{"type": "command", "command": "foreign"}]}
    hp.write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}), encoding="utf-8")
    config = hp.with_name("config.toml")
    config.write_text('model = "operator-choice"\n', encoding="utf-8")
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    before = {p: p.read_bytes() for p in hp.parent.rglob("*") if p.is_file()}
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    assert before == {p: p.read_bytes() for p in hp.parent.rglob("*") if p.is_file()}
    assert foreign in json.loads(hp.read_text())["hooks"]["SessionStart"]
    assert 'model = "operator-choice"\n' in config.read_text()


def test_strict_host_reports_completed_operations_on_apply_repeat_and_failure(runtime_box, monkeypatch):
    root, hooks = runtime_box
    out = io.StringIO()
    assert ui.enroll_codex_runtime(root, consent=True, out=out) == 0
    assert hooks.exists()
    assert "hooks=created" in out.getvalue()
    assert "exec rule created" in out.getvalue()
    assert "example-retrieval-repo MCP written" in out.getvalue()

    out = io.StringIO()
    assert ui.enroll_codex_runtime(root, consent=True, out=out) == 0
    assert "hooks=present" in out.getvalue()
    assert "exec rule present" in out.getvalue()
    assert "example-retrieval-repo MCP current" in out.getvalue()

    hooks.unlink()
    monkeypatch.setattr(ui, "trust_codex_hook", lambda *a, **kw: (False, "trust failed"))
    out = io.StringIO()
    assert ui.enroll_codex_runtime(root, consent=True, out=out) == 5
    assert hooks.exists()
    assert "hooks=created; trust failed" in out.getvalue()
    assert "Codex runtime ready" not in out.getvalue()


def test_runtime_check_is_read_only(runtime_box, monkeypatch):
    root, hp = runtime_box
    monkeypatch.setattr(ui, "install_scripts", lambda *a: pytest.fail("must not copy"))
    monkeypatch.setattr(ui, "trust_codex_hook", lambda *a: pytest.fail("must not change trust"))
    out = io.StringIO()
    assert ui.enroll_codex_runtime(root, check=True, out=out) == 0
    assert "read-only" in out.getvalue()
    assert "runtime ready" not in out.getvalue()
    assert not hp.parent.exists()


@pytest.mark.parametrize("failure", ["missing", "empty", "conflict"])
def test_runtime_skill_failure_withholds_ready_and_allows_retry(runtime_box, monkeypatch, failure):
    root, hp = runtime_box
    monkeypatch.setattr(ui, "_seed_standing_doctrine", _REAL_SEED_STANDING_DOCTRINE)
    source = root / "skills"
    monkeypatch.setattr(ui, "SKILLS_ROOT", source)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    dest = Path.home() / ".agents" / "skills"
    if failure != "missing":
        source.mkdir(parents=True)
    if failure == "conflict":
        (source / "one").mkdir()
        (source / "one" / "SKILL.md").write_text("required")
        (dest / "one").mkdir(parents=True)
        (dest / "one" / "SKILL.md").write_text("foreign")
    out = io.StringIO()
    assert ui.enroll_codex_runtime(root, consent=True, out=out) == 7
    assert "incomplete" in out.getvalue() and "Codex runtime ready" not in out.getvalue()
    if failure == "conflict":
        assert (dest / "one" / "SKILL.md").read_text() == "foreign"
        (dest / "one").rename(dest / "operator-one")
    else:
        (source / "one").mkdir(parents=True)
        (source / "one" / "SKILL.md").write_text("required")
    out = io.StringIO()
    assert ui.enroll_codex_runtime(root, consent=True, out=out) == 0
    assert "Codex runtime ready" in out.getvalue()


@pytest.mark.parametrize("command", [
    'echo "unimatrix-codex-hook-sessionstart.py"',
    '"/foreign tool/echo" "unimatrix-host-guard.py"',
    'python -c "print(123)" unimatrix-codex-hook-sessionstart.py',
    'python foreign.py unimatrix-codex-hook-sessionstart.py',
    'echo unimatrix-host-guard.py.backup',
])
def test_codex_hook_merge_and_uninstall_preserve_argument_mentions(tmp_path, command):
    hp = tmp_path / "hooks.json"
    foreign = {"matcher": "*", "operator": "retain", "hooks": [{"type": "command", "command": command}]}
    owned = ui.codex_hook_events(tmp_path / "new path", "old-engine")["SessionStart"]
    mixed = {**foreign, "hooks": foreign["hooks"] + owned["hooks"]}
    hp.write_text(json.dumps({"operator": True, "hooks": {"SessionStart": [foreign, mixed]}}))
    ui.seed_codex_hook(hp, tmp_path / "new path")
    assert json.loads(hp.read_text())["hooks"]["SessionStart"][:2] == [foreign, foreign]
    assert ui.seed_codex_hook(hp, tmp_path / "new path") == "present"
    ui.seed_codex_hook(hp, tmp_path / "new path", "new-engine")
    assert ui.unseed_codex_hook(hp, tmp_path / "new path") == "removed"
    assert json.loads(hp.read_text()) == {"operator": True, "hooks": {"SessionStart": [foreign, foreign]}}


@pytest.mark.parametrize("windows", [False, True])
def test_codex_hook_full_script_identity_preserves_foreign_groups(tmp_path, windows):
    hp = tmp_path / "hooks.json"
    dest = tmp_path / "installed path"
    script = ui.CODEX_SESSIONSTART_SCRIPT
    if windows:
        foreign_command = f'"C:\\Python Home\\python.exe" "C:\\foreign path\\{script}"'
    else:
        foreign_command = f'python "/foreign path/{script}"'
    foreign = {"matcher": "*", "operator": {"keep": True}, "hooks": [
        {"type": "command", "command": foreign_command}]}
    # Earlier installers emitted Windows backslashes and double-quoted script paths.
    legacy = {"type": "command", "command": f'python "{str(dest / script).replace(chr(47), chr(92))}"'}
    mixed = {**foreign, "hooks": [*foreign["hooks"], legacy]}
    hp.write_text(json.dumps({"hooks": {"SessionStart": [foreign, mixed]}}))
    ui.seed_codex_hook(hp, dest)
    assert _hooks_file(hp)["hooks"]["SessionStart"][:2] == [foreign, foreign]
    assert ui.seed_codex_hook(hp, dest) == "present"
    assert len(ui._codex_expected_handlers(hp, dest)) == 11
    assert ui.unseed_codex_hook(hp, dest) == "removed"
    assert _hooks_file(hp) == {"hooks": {"SessionStart": [foreign, foreign]}}


def test_codex_hook_windows_target_uses_full_case_insensitive_identity():
    dest = r'C:\Users\Operator\Settings Home\unimatrix'
    handler = {"command": r'"C:\Python Home\python.exe" -u "c:\users\operator\settings home\unimatrix\unimatrix-codex-hook-sessionstart.py"'}
    assert ui._codex_handler_script(handler, dest) == ui.CODEX_SESSIONSTART_SCRIPT
    handler["command"] = handler["command"].replace('settings home', 'foreign home')
    assert ui._codex_handler_script(handler, dest) is None


@pytest.mark.parametrize("uninstall", [False, True])
def test_codex_hooks_detect_intervening_foreign_edit(tmp_path, monkeypatch, uninstall):
    hp = tmp_path / "hooks.json"
    ui.seed_codex_hook(hp, tmp_path / "initial")
    original = ui._codex_ours
    foreign = {"hooks": {"SessionStart": [{"hooks": [{"command": "foreign edit"}]}]}}

    def edited(group, dest_dir=None):
        hp.write_text(json.dumps(foreign))
        return original(group, dest_dir)

    monkeypatch.setattr(ui, "_codex_ours", edited)
    with pytest.raises(ValueError, match="drift"):
        if uninstall:
            ui.unseed_codex_hook(hp, tmp_path / "initial")
        else:
            ui.seed_codex_hook(hp, tmp_path / "new")
    assert json.loads(hp.read_text()) == foreign


def test_runtime_enrollment_uses_selected_homes_and_leaves_defaults(runtime_box, monkeypatch):
    root, hp = runtime_box
    default = Path.home() / ".codex"
    default.mkdir()
    sentinel = default / "config.toml"
    sentinel.write_bytes(b'model = "default-home-sentinel"\n')
    default_settings = Path.home() / ".coordinator-claude-settings"
    default_settings.mkdir()
    settings_sentinel = default_settings / "sentinel"
    settings_sentinel.write_bytes(b"unchanged")
    copied = []
    monkeypatch.setattr(ui, "install_scripts", lambda src, dest, on_copy=None: copied.append(dest))

    assert ui.enroll_codex_runtime(root, check=True) == 0
    assert not hp.exists() and copied == []
    assert ui.enroll_codex_runtime(root, consent=True) == 0
    assert copied == [Path(os.environ["COORDINATOR_SETTINGS_HOME"]) / "unimatrix"]
    assert hp.is_file()
    assert "[mcp_servers.project-rag]" in hp.with_name("config.toml").read_text()
    assert sentinel.read_bytes() == b'model = "default-home-sentinel"\n'
    assert list(default.iterdir()) == [sentinel]
    assert settings_sentinel.read_bytes() == b"unchanged"
    assert list(default_settings.iterdir()) == [settings_sentinel]


@pytest.mark.parametrize("failure", ["absent", "lifecycle", "trust", "doctrine", "malformed", "mcp-drift"])
def test_runtime_readiness_failure_is_nonzero(runtime_box, monkeypatch, failure):
    root, hp = runtime_box
    if failure == "absent":
        monkeypatch.setattr(ui, "host_present", lambda *a: False)
    elif failure == "lifecycle":
        monkeypatch.setattr(ui, "external_session_capability", lambda *a: (False, "missing registration"))
    elif failure == "trust":
        monkeypatch.setattr(ui, "trust_codex_hook", lambda *a, **kw: (False, "untrusted"))
    elif failure == "doctrine":
        monkeypatch.setattr(ui, "_seed_standing_doctrine", lambda *a: ["doctrine"])
    else:
        hp.parent.mkdir()
        hp.with_name("config.toml").write_text("[broken" if failure == "malformed" else '[mcp_servers.project-rag]\nurl = "foreign"\n', encoding="utf-8")
    assert ui.enroll_codex_runtime(root, consent=True) != 0
    if failure in ("absent", "lifecycle", "malformed", "mcp-drift"):
        assert not hp.exists()


def test_enrolled_update_uses_shared_runtime_callable(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(ui, "enroll_codex_runtime", lambda root, **kw: seen.append((root, kw)) or 7)
    assert ui.main(["--install", "--hosts", "codex", "--enroll-runtime"], out=io.StringIO()) == 7
    assert len(seen) == 1 and seen[0][1]["consent"] is True


def test_codex_seed_is_idempotent_and_keeps_foreign_hooks(tmp_path):
    hp = tmp_path / "hooks.json"
    foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}], "Stop": []}}
    hp.write_text(json.dumps(foreign), encoding="utf-8")
    assert ui.seed_codex_hook(hp, tmp_path) == "added"
    once = hp.read_text(encoding="utf-8")
    assert ui.seed_codex_hook(hp, tmp_path) == "present"
    assert hp.read_text(encoding="utf-8") == once
    groups = json.loads(once)["hooks"]["SessionStart"]
    assert groups[0] == foreign["hooks"]["SessionStart"][0] and len(groups) == 2
    assert _fwd(tmp_path / ui.CODEX_SESSIONSTART_SCRIPT) in _shown(groups[1]["hooks"][0]["command"])


def test_codex_seed_creates_file_and_unseed_restores_foreign(tmp_path):
    hp = tmp_path / "codex" / "hooks.json"
    assert ui.seed_codex_hook(hp, tmp_path) == "created"
    assert ui.unseed_codex_hook(hp, tmp_path) == "removed"
    assert json.loads(hp.read_text(encoding="utf-8")) == {}
    assert ui.unseed_codex_hook(hp, tmp_path) == "absent"
    foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}
    hp.write_text(json.dumps(foreign), encoding="utf-8")
    ui.seed_codex_hook(hp, tmp_path)
    assert ui.unseed_codex_hook(hp, tmp_path) == "removed"
    assert json.loads(hp.read_text(encoding="utf-8")) == foreign


@pytest.mark.parametrize("raw", ["{bad", "[1]", '{"hooks": [1]}', '{"hooks": {"SessionStart": 3}}'])
def test_codex_seed_refuses_unusable_file_untouched(tmp_path, raw):
    hp = tmp_path / "hooks.json"
    hp.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        ui.seed_codex_hook(hp, tmp_path)
    assert hp.read_text(encoding="utf-8") == raw


def test_install_seeds_both_and_trusts_codex(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    hp, chp = tmp_path / "gemini" / "hooks.json", tmp_path / "codex" / "hooks.json"
    trusted = []
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=dest, out=buf, hooks_path=hp,
                 codex_hooks=chp, codex_trust=lambda p: trusted.append(p) or (True, "trusted 1 hook(s)"))
    assert rc == 0 and "codex hook created" in buf.getvalue() and "codex hook trust" in buf.getvalue()
    assert trusted == [chp]
    assert "SessionStart" in json.loads(hp.read_text(encoding="utf-8"))[ui.AGY_HOOK_KEY]
    cmd = json.loads(chp.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert _fwd(dest / ui.CODEX_SESSIONSTART_SCRIPT) in _shown(cmd)


def test_install_fails_nonzero_when_codex_trust_cannot_be_verified(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf,
                 hooks_path=tmp_path / "h.json", codex_hooks=tmp_path / "c.json",
                 codex_trust=lambda p: (False, "codex not on PATH"))
    assert rc != 0 and "governance verification FAILED: codex not on PATH" in buf.getvalue()


def test_install_with_only_agy_path_never_touches_real_codex(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "real-codex"))
    monkeypatch.setattr(ui, "trust_codex_hook", lambda p: pytest.fail("real trust must not run"))
    rc, _ = _run(["--install"], "2.1.316", tmp_path / "d", tmp_path / "gemini" / "hooks.json")
    assert rc == 0
    assert not (tmp_path / "real-codex").exists()
    assert (tmp_path / "gemini" / "codex-hooks.json").exists()


def test_install_refuses_unusable_codex_hooks(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    chp = tmp_path / "c.json"
    chp.write_text("{bad", encoding="utf-8")
    rc, out = _run_codex(tmp_path, chp)
    assert rc == 4 and "codex hook not installed" in out
    assert chp.read_text(encoding="utf-8") == "{bad"


def _run_codex(tmp_path, chp):
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf,
                 hooks_path=tmp_path / "h.json", codex_hooks=chp, codex_trust=lambda p: (True, "x"))
    return rc, buf.getvalue()


def test_uninstall_removes_codex_hook_only(tmp_path):
    chp = tmp_path / "c.json"
    foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}
    chp.write_text(json.dumps(foreign), encoding="utf-8")
    ui.seed_codex_hook(chp, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--uninstall"], out=buf, hooks_path=tmp_path / "h.json", codex_hooks=chp, dest_dir=tmp_path)
    assert rc == 0 and "codex hook removed" in buf.getvalue()
    assert json.loads(chp.read_text(encoding="utf-8")) == foreign


def test_trust_codex_hook_without_codex_reports_not_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    ok, note = ui.trust_codex_hook(tmp_path / "hooks.json")
    assert ok is False and "codex not on PATH" in note


@pytest.mark.skipif(not ui.shutil.which("codex"), reason="codex CLI not installed")
@pytest.mark.parametrize("home_name", ["plain", "space and \U0001f680"])
def test_trust_codex_hook_marks_hook_trusted_in_real_codex(tmp_path, home_name):
    tmp_path = tmp_path / home_name
    tmp_path.mkdir()
    hp = tmp_path / "hooks.json"
    ui.seed_codex_hook(hp, tmp_path)
    ok, note = ui.trust_codex_hook(hp, dest_dir=tmp_path)
    assert ok, note
    cfg = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "trusted_hash" in cfg and "session_start" in cfg
    states = tomllib.loads(cfg)["hooks"]["state"]
    assert set(states) == {
        f"{hp}:pre_tool_use:0:0", f"{hp}:session_start:0:0",
        f"{hp}:session_start:0:1", f"{hp}:session_end:0:0", f"{hp}:subagent_start:0:0",
        *(f"{hp}:{event}:0:0" for event in (
            "user_prompt_submit", "post_tool_use", "stop", "subagent_stop", "pre_compact", "post_compact")),
    }
    assert all(row["trusted_hash"].startswith("sha256:") for row in states.values())


def _home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(ui, "trust_codex_hook", lambda p, **kw: (True, "x"))
    return home


def test_default_install_writes_doctrine_block_for_codex_and_agy(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    dest = tmp_path / "dest"
    rc, out = _run(["--install"], "2.1.316", dest)
    assert rc == 0 and "codex peer doctrine created" in out and "agy peer doctrine created" in out
    for p in (home / ".codex" / "AGENTS.md", home / ".gemini" / "GEMINI.md"):
        text = p.read_text(encoding="utf-8")
        assert ui.DOCTRINE_BEGIN in text and ui.DOCTRINE_END in text
        assert _fwd(dest / ui.SEND_SCRIPT) in _shown(text) and "{SEND}" not in text
        assert "--to <sender>" in text and "--list" in text
        assert "`coordinator-invoke engine_ask.submit --params-file .coordinator-local/em-submit.json`" in text and "{INVOKE}" not in text


def test_invoke_launcher_path_suffix_follows_platform(monkeypatch):
    monkeypatch.setattr(ui.sys, "platform", "win32")
    assert ui.invoke_launcher_path().endswith("coordinator-invoke.exe")
    monkeypatch.setattr(ui.sys, "platform", "linux")
    assert ui.invoke_launcher_path().endswith("coordinator-invoke") and not ui.invoke_launcher_path().endswith(".exe")


def test_doctrine_reinstall_is_idempotent_and_keeps_foreign_text(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("# mine\nkeep me\n", encoding="utf-8")
    block = ui.render_peer_doctrine(tmp_path)
    assert ui.seed_peer_doctrine(p, block) == "added"
    first = p.read_text(encoding="utf-8")
    assert first.startswith("# mine\nkeep me\n\n") and first.count(ui.DOCTRINE_BEGIN) == 1
    assert ui.seed_peer_doctrine(p, block) == "present"
    assert p.read_text(encoding="utf-8") == first
    assert ui.seed_peer_doctrine(p, block.replace("colleague", "peer")) == "updated"
    assert p.read_text(encoding="utf-8").count(ui.DOCTRINE_BEGIN) == 1


def test_doctrine_uninstall_removes_only_the_block(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("before\n", encoding="utf-8")
    ui.seed_peer_doctrine(p, ui.render_peer_doctrine(tmp_path))
    assert ui.unseed_peer_doctrine(p) == "removed"
    assert p.read_text(encoding="utf-8") == "before\n"
    assert ui.unseed_peer_doctrine(p) == "absent"


def test_doctrine_uninstall_deletes_file_it_created(tmp_path):
    p = tmp_path / "sub" / "GEMINI.md"
    assert ui.seed_peer_doctrine(p, ui.render_peer_doctrine(tmp_path)) == "created"
    assert ui.unseed_peer_doctrine(p) == "removed"
    assert not p.exists()
    assert ui.unseed_peer_doctrine(p) == "absent"


def test_main_uninstall_strips_doctrine_from_default_files(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.md").write_text("theirs\n", encoding="utf-8")
    assert _run(["--install"], "2.1.316", tmp_path / "d")[0] == 0
    buf = io.StringIO()
    assert ui.main(["--uninstall"], out=buf) == 0
    assert (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8") == "theirs\n"
    assert not (home / ".gemini" / "GEMINI.md").exists()
    assert "codex peer doctrine removed" in buf.getvalue()


def test_grok_install_seeds_peer_doctrine_into_grok_home_rules_and_uninstall_removes_it(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grokhome"))
    rules = tmp_path / "grokhome" / "rules" / "unimatrix-peer-doctrine.md"
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf) == 0
    assert "grok peer doctrine created" in buf.getvalue()
    text = rules.read_text(encoding="utf-8")
    assert "engine_ask.submit" in text and ui.DOCTRINE_BEGIN in text and "{SEND}" not in text
    assert _fwd(tmp_path / "dest" / ui.SEND_SCRIPT) in _shown(text)
    assert "`coordinator-invoke engine_ask.submit --params-file .coordinator-local/em-submit.json`" in text and "{INVOKE}" not in text
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf) == 0
    assert "grok peer doctrine present" in buf.getvalue() and rules.read_text(encoding="utf-8") == text
    buf = io.StringIO()
    assert ui.main(["--uninstall", "--hosts", "grok"], dest_dir=tmp_path / "dest", out=buf) == 0
    assert "grok peer doctrine removed" in buf.getvalue() and not rules.exists()


def test_default_install_seeds_grok_peer_doctrine_under_home_grok(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    rc, out = _run(["--install"], "2.1.316", tmp_path / "dest")
    assert rc == 0 and "grok peer doctrine created" in out
    assert "engine_ask.submit" in (home / ".grok" / "rules" / "unimatrix-peer-doctrine.md").read_text(encoding="utf-8")


def test_missing_peer_doctrine_template_flags_grok_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "PEER_DOCTRINE_TEMPLATE", tmp_path / "absent-peer-doctrine.md")
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    _stub_src(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--install", "--hosts", "grok"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf)
    assert rc == 7 and "not installed: grok peer doctrine" in buf.getvalue()


def test_install_copies_send_script_when_present(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    (tmp_path / "src" / "unimatrix-send.py").write_text("# send\n", encoding="utf-8")
    assert _run(["--install"], "2.1.316", tmp_path / "d")[0] == 0
    assert (tmp_path / "d" / "unimatrix-send.py").is_file()


def test_override_hooks_path_never_touches_real_home_doctrine(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    rc, _ = _run(["--install"], "2.1.316", tmp_path / "d", tmp_path / "g" / "hooks.json")
    assert rc == 0
    assert not (home / ".codex").exists() and not (home / ".gemini").exists()
    assert (tmp_path / "g" / "codex-AGENTS.md").exists() and (tmp_path / "g" / "agy-GEMINI.md").exists()


def test_agy_seed_repoints_a_stale_script_path(tmp_path):
    hp = tmp_path / "hooks.json"
    old = ui.agy_hook_entry(tmp_path / "old-install")
    hp.write_text(json.dumps({ui.AGY_HOOK_KEY: old}), encoding="utf-8")
    assert ui.seed_agy_hook(hp, tmp_path / "new") == "updated"
    assert json.loads(hp.read_text(encoding="utf-8"))[ui.AGY_HOOK_KEY] == ui.agy_hook_entry(tmp_path / "new")


def test_codex_send_rule_allows_only_the_send_script_and_uninstalls(tmp_path):
    rule = ui.render_codex_exec_rule(tmp_path)
    assert "prefix_rule(pattern=" in rule and 'decision="allow"' in rule
    assert json.dumps(_fwd(tmp_path / ui.SEND_SCRIPT)) in rule and rule.count("prefix_rule") == 1
    p = tmp_path / "rules" / "unimatrix.rules"
    assert ui.seed_file(p, rule) == "created" and ui.seed_file(p, rule) == "present"
    assert ui.unseed_file(p) == "removed" and not p.exists() and ui.unseed_file(p) == "absent"


def _hooks_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _default_install(monkeypatch, tmp_path, argv=("--install",)):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(list(argv), version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf)
    return home, rc, buf.getvalue()


def test_default_install_registers_guards_doctrine_and_skills(monkeypatch, tmp_path, _engine):
    # claude-klabauter carries no global-doctrine/ tree: a minimal one stands in for the doctrine source.
    doc = tmp_path / "doctrine-root" / "global-doctrine"
    (doc / "rules").mkdir(parents=True)
    (doc / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    (doc / "rules" / "rules.md").write_text("- a rule\n", encoding="utf-8")
    monkeypatch.setattr(ui, "doctrine_root", lambda: tmp_path / "doctrine-root")
    (tmp_path / "skills" / "one").mkdir(parents=True)
    (tmp_path / "skills" / "one" / "SKILL.md").write_text("# one\n", encoding="utf-8")
    monkeypatch.setattr(ui, "SKILLS_ROOT", tmp_path / "skills")
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 0 and "governance DEGRADED" not in out
    guard = _hooks_file(home / ".codex" / "hooks.json")["hooks"]["PreToolUse"][0]
    assert guard["matcher"] == ".*"
    handler = guard["hooks"][0]
    assert "unimatrix-host-guard.py" in handler["command"] and "--host codex" in _shown(handler["command"])
    assert _fwd(_engine) in _shown(handler["command"]) and isinstance(handler["timeout"], int)
    agy = _hooks_file(home / ".gemini" / "config" / "hooks.json")[ui.AGY_HOOK_KEY]
    assert len(agy["PreToolUse"]) == 1 and _fwd(_engine) in _shown(agy["PreToolUse"][0]["hooks"][0]["command"])
    assert "hooks" not in agy["SessionStart"][0] and agy["SessionStart"][0]["type"] == "command"
    for rules in (home / ".codex" / "AGENTS.md", home / ".gemini" / "GEMINI.md"):
        text = rules.read_text(encoding="utf-8")
        assert ui.DOCTRINE_BEGIN in text and "<!-- BEGIN coordinator-doctrine" in text
    assert not (home / ".gemini" / "config" / "skills.json").exists()
    assert any((home / ".agents" / "skills").glob("*.coordinator-managed"))
    assert "governance scope: Codex local-tool PreToolUse" in out


def test_unresolved_engine_root_is_degraded_without_engine_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (None, "unresolved"))
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 7 and "governance DEGRADED: engine root unresolved" in out
    assert "governance verified" not in out
    cmd = _hooks_file(home / ".codex" / "hooks.json")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--engine-root" not in cmd


def test_uninstall_removes_everything_and_keeps_foreign(monkeypatch, tmp_path):
    home, _, _ = _default_install(monkeypatch, tmp_path)
    chp = home / ".codex" / "hooks.json"
    data = _hooks_file(chp)
    data["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs"}]})
    chp.write_text(json.dumps(data), encoding="utf-8")
    (home / ".codex" / "AGENTS.md").write_text(
        "mine\n" + (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8")
    buf = io.StringIO()
    assert ui.main(["--uninstall"], out=buf, dest_dir=tmp_path / "dest") == 0
    left = _hooks_file(chp)["hooks"]
    assert left == {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs"}]}]}
    assert (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"
    assert not (home / ".gemini" / "GEMINI.md").exists()
    assert not (home / ".gemini" / "config" / "skills.json").exists()
    assert not list((home / ".agents" / "skills").glob("*"))
    assert ui.AGY_HOOK_KEY not in _hooks_file(home / ".gemini" / "config" / "hooks.json")


def test_reinstall_with_changed_engine_root_updates_guard(monkeypatch, tmp_path):
    home, _, _ = _default_install(monkeypatch, tmp_path)
    other = tmp_path / "engine2"
    other.mkdir()
    package = other / "coordinator_core" / "session"
    package.mkdir(parents=True)
    (other / "coordinator_core" / "_engine_stamp").write_text("test-stamp", encoding="utf-8")
    (package / "external_registry.py").write_text("# capability fixture", encoding="utf-8")
    (package.parent / "host_hooks").mkdir(exist_ok=True)
    (package.parent / "host_hooks" / "session_context.py").write_text("# capability fixture", encoding="utf-8")
    (package.parent / "host_hooks" / "codex_lifecycle.py").write_text("# capability fixture", encoding="utf-8")
    (package.parent / "codex_status").mkdir(exist_ok=True)
    (package.parent / "codex_status" / "hook.py").write_text("# capability fixture", encoding="utf-8")
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (str(other), "published-target"))
    buf = io.StringIO()
    assert ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf) == 0
    codex = _hooks_file(home / ".codex" / "hooks.json")["hooks"]
    assert len(codex["PreToolUse"]) == 1 and _fwd(other) in _shown(codex["PreToolUse"][0]["hooks"][0]["command"])
    assert "codex hook updated" in buf.getvalue()


def test_plain_install_no_claude_on_path_hosts_skip_gate_but_bare_install_refuses(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    buf = io.StringIO()
    assert ui.main(["--install"], dest_dir=tmp_path / "d", out=buf) == 2
    assert not (home / ".codex").exists()
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "codex,agy"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "gate skipped" in buf.getvalue() and (home / ".codex" / "hooks.json").exists()
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "claude,codex"], dest_dir=tmp_path / "d", out=buf) == 2


def _fwd(path):
    return str(path).replace("\\", "/")


def _shown(command):
    """A hook command with its single quotes dropped, to assert on its words."""
    return command.replace("'", "")


def _live_shell(name):
    exe = shutil.which(name)
    if not exe:
        pytest.skip(f"{name} not on PATH")
    flags = ["-NoProfile", "-NonInteractive", "-Command"] if name == "pwsh" else ["-c"]
    return exe, flags


def _run_in_shell(name, command, env):
    exe, flags = _live_shell(name)
    probe = subprocess.run([exe, *flags, "python -c pass"], env=env, capture_output=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"{name} cannot reach python on this box")
    done = subprocess.run([exe, *flags, command], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
def test_hook_command_parses_to_one_argv_under_bash_and_pwsh(monkeypatch, tmp_path, shell):
    _live_shell(shell)
    dest = tmp_path / "dest dir"
    dest.mkdir()
    (dest / "echo_argv.py").write_text("import json,sys;print(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    py = Path(sys.executable)
    monkeypatch.setattr(ui, "_console_python", lambda: py.name)
    env = dict(os.environ, PATH=str(py.parent) + os.pathsep + os.environ.get("PATH", ""))
    engine = str(tmp_path / "engine root").replace("/", os.sep)
    command = ui._hook_command(dest, "echo_argv.py", "--host", "grok", *ui._engine_args(engine))
    assert "\\" not in command
    assert _run_in_shell(shell, command, env) == ["--host", "grok", "--engine-root", engine.replace("\\", "/")]


def test_interpreter_with_a_space_takes_its_short_form_and_is_quoted_only_without_one(monkeypatch):
    spaced = "\\opt\\Program Files\\py\\python.exe"
    monkeypatch.setattr(ui, "_short_path", lambda exe: "\\opt\\PROGRA~1\\py\\python.exe")
    assert ui._neutral_command([spaced, "x.py"]) == "/opt/PROGRA~1/py/python.exe 'x.py'"
    monkeypatch.setattr(ui, "_short_path", lambda exe: exe)
    assert ui._neutral_command([spaced, "x.py"]) == "\"/opt/Program Files/py/python.exe\" 'x.py'"


def test_tui_status_command_has_no_backslash_for_bash_to_strip(monkeypatch):
    monkeypatch.setattr(ui, "_console_python", lambda: "\\py\\python.exe")
    for root in ("\\eng\\root", None):
        cmd = ui._tui_status_command("grok", root)
        assert "\\" not in cmd
        assert ui._status_command_ours({"command": cmd}, "grok")


def test_tui_status_command_quotes_short_and_spaced_arguments(monkeypatch):
    spaced = "C:\\Program Files\\py\\python.exe"
    monkeypatch.setattr(ui, "_console_python", lambda: spaced)
    monkeypatch.setattr(ui, "_short_path", lambda p: p.replace("Program Files", "PROGRA~1"))
    cmd = ui._tui_status_command("agy", "C:\\Program Files\\eng", "C:\\Program Files\\u")
    assert shlex.split(cmd) == ["C:/PROGRA~1/py/python.exe", "C:/PROGRA~1/u/unimatrix-tui-status-launch.py", "C:/PROGRA~1/eng", "agy"]
    assert "-c" not in cmd.split()
    monkeypatch.setattr(ui, "_short_path", lambda p: p)
    assert shlex.split(ui._tui_status_command("agy", "C:\\Program Files\\eng", "C:\\Program Files\\u")) == ["C:/Program Files/py/python.exe", "C:/Program Files/u/unimatrix-tui-status-launch.py", "C:/Program Files/eng", "agy"]


def test_hook_command_is_forward_slash_quoted_and_old_forms_are_still_claimed(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "_console_python", lambda: "python.exe")
    command = ui._hook_command("u\\me\\.coord", ui.GUARD_SCRIPT, "--host", "grok", "--engine-root", "d\\e")
    assert command == "python.exe 'u/me/.coord/unimatrix-host-guard.py' '--host' 'grok' '--engine-root' 'd/e'"
    old = [r'python.exe u\me\.coord\unimatrix-host-guard.py --host grok --engine-root d\e',
           "python 'x/unimatrix-host-guard.py' --host grok"]
    for cmd in (command, *old):
        group = {"hooks": [{"type": "command", "command": cmd}]}
        assert ui._grok_group_ours(group)
        script_path = shlex.split(cmd, posix="\\" not in cmd)[1].strip("\"'").replace("\\", "/")
        assert ui._codex_ours({"hooks": [{"type": "command", "command": cmd.replace("grok", "codex")}]}, Path(script_path).parent)
    assert not ui._grok_group_ours({"hooks": [{"command": command.replace("grok", "codex")}]})


def _grok_guard_commands(hooks_file):
    groups = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    return [(g.get("matcher"), h["command"]) for g in groups for h in g["hooks"]]


def test_hosts_grok_ships_the_adapter_and_registers_only_the_grok_pretooluse_hook(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    (home / ".grok").mkdir()
    dest = tmp_path / "d"
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], dest_dir=dest, out=buf) == 0
    assert (dest / "unimatrix-peer-grok.py").is_file()
    assert not (home / ".codex" / "hooks.json").exists()
    assert not (home / ".gemini" / "config" / "hooks.json").exists()
    assert not (home / ".gemini").exists()
    assert tomllib.loads((home / ".grok" / "config.toml").read_text())["ui"]["status_line"]["type"] == "command"
    assert not (home / ".grok" / "hooks.json").exists()
    ((matcher, command),) = _grok_guard_commands(home / ".grok" / "hooks" / "coordinator.json")
    assert "unimatrix-host-guard.py" in command and "--host grok" in _shown(command)
    for tool in ("Bash", "run_command", "Edit", "Write", "MultiEdit", "NotebookEdit", "edit_file", "write_file",
                 "read_file", "spawn_subagent"):
        assert tool in matcher.split("|")
    assert "grok hook created" in buf.getvalue()
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok,bogus"], dest_dir=dest, out=buf) == 2


def test_default_hosts_register_grok_hook_under_hooks_path_and_leave_grok_home_alone(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    hp = tmp_path / "cfg" / "h.json"
    hp.parent.mkdir()
    chp = tmp_path / "cfg" / "c.json"
    dest = tmp_path / "d"
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=dest, out=buf, hooks_path=hp,
                 codex_hooks=chp, codex_trust=lambda p: (True, "x"))
    assert rc in (0, 7), buf.getvalue()
    assert list(grok_home.iterdir()) == []
    ((_, command),) = _grok_guard_commands(hp.with_name("grok-hooks.json"))
    assert "unimatrix-host-guard.py" in command and "--host grok" in _shown(command)
    # codex's own guard entry is a different host's and is not the grok one
    assert "--host codex" in _shown(chp.read_text(encoding="utf-8"))


def test_uninstall_removes_only_the_grok_hook_entry(tmp_path):
    ghp = tmp_path / "grok-hooks.json"
    foreign = {"theme": "x", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "theirs"}]}], "SessionStart": [{"hooks": [
            {"type": "command", "command": "theirs-too"}]}]}}
    ghp.write_text(json.dumps(foreign, indent=2) + "\n", encoding="utf-8")
    assert ui.seed_grok_hook(ghp, tmp_path) == "added"
    assert ui.seed_grok_hook(ghp, tmp_path) == "present"
    assert len(_grok_guard_commands(ghp)) == 2
    buf = io.StringIO()
    rc = ui.main(["--uninstall", "--hosts", "grok"], out=buf, hooks_path=tmp_path / "h.json")
    assert rc == 0 and "grok hook removed" in buf.getvalue()
    assert json.loads(ghp.read_text(encoding="utf-8")) == foreign


def _stale_grok_file(path, foreign):
    old = ui.grok_hook_entry(path.parent, "/engine")
    old["matcher"] = old["matcher"].replace("|search_replace", "").replace("search_replace|", "")
    old["hooks"][0]["command"] = old["hooks"][0]["command"].replace('"', "").replace("/", "\\")
    assert old["matcher"] != ui.GROK_TOOL_MATCHER
    path.write_text(json.dumps({"hooks": {"PreToolUse": [foreign, old]}}, indent=2) + "\n", encoding="utf-8")


def test_refresh_grok_hook_rerenders_a_stale_group_keeps_foreign_and_leaves_a_current_file_alone(tmp_path):
    ghp = tmp_path / "hooks" / "coordinator.json"
    ghp.parent.mkdir()
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs"}]}
    _stale_grok_file(ghp, foreign)
    assert ui.refresh_grok_hook(ghp, tmp_path, "/engine") == "updated"
    assert json.loads(ghp.read_text(encoding="utf-8"))["hooks"]["PreToolUse"] == [
        foreign, ui.grok_hook_entry(tmp_path, "/engine")]
    mtime = ghp.stat().st_mtime_ns
    raw = ghp.read_bytes()
    assert ui.refresh_grok_hook(ghp, tmp_path, "/engine") == "present"
    assert ghp.read_bytes() == raw and ghp.stat().st_mtime_ns == mtime


def test_refresh_grok_hook_creates_nothing_and_skips_an_unresolved_engine(tmp_path):
    ghp = tmp_path / "hooks.json"
    assert ui.refresh_grok_hook(ghp, tmp_path, "/engine") == "absent" and not ghp.exists()
    theirs = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}) + "\n"
    ghp.write_text(theirs, encoding="utf-8")
    assert ui.refresh_grok_hook(ghp, tmp_path, "/engine") == "absent" and ghp.read_text(encoding="utf-8") == theirs
    _stale_grok_file(ghp, {"hooks": []})
    raw = ghp.read_bytes()
    assert ui.refresh_grok_hook(ghp, tmp_path, None) == "absent" and ghp.read_bytes() == raw


def test_main_refresh_hooks_rerenders_a_stale_grok_file_without_install(tmp_path):
    hooks = tmp_path / "h.json"
    ghp = hooks.with_name("grok-hooks.json")
    _stale_grok_file(ghp, {"hooks": []})
    buf = io.StringIO()
    dest = tmp_path / "unimatrix"
    rc = ui.main(["--refresh-hooks"], out=buf, hooks_path=hooks, dest_dir=dest)
    assert rc == 0 and "grok hook updated" in buf.getvalue()
    engine = ui.resolve_engine_root()[0]
    assert json.loads(ghp.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][-1] == ui.grok_hook_entry(dest, engine)
    assert not dest.exists()


def test_seed_grok_hook_heals_a_stale_engine_root_and_uninstall_empties_it(tmp_path):
    ghp = tmp_path / "hooks.json"
    assert ui.seed_grok_hook(ghp, tmp_path, "/old") == "created"
    assert ui.seed_grok_hook(ghp, tmp_path, "/new") == "updated"
    ((_, command),) = _grok_guard_commands(ghp)
    assert "/new" in command and "/old" not in command
    assert ui.unseed_grok_hook(ghp) == "removed"
    assert json.loads(ghp.read_text(encoding="utf-8")) == {}
    assert ui.unseed_grok_hook(ghp) == "absent"


class _FakeCodex:
    """A codex app-server stand-in: hooks/list rows from `rows`, config/value/write marks them trusted."""

    def __init__(self, hooks_file, commands, trust_works=True, disabled=False, hide_guard=False,
                 hide_session=False, relist=None, rpc_error=None):
        self.out = []
        self.rows = []
        self.trust_works = trust_works
        self.relist = relist
        self.rpc_error = rpc_error
        self.lists = 0
        for i, (event, cmd) in enumerate(commands):
            if hide_guard and "host-guard" in cmd:
                continue
            if hide_session and event == "sessionStart":
                continue
            self.rows.append({"key": f"k{i}", "eventName": event, "command": cmd, "sourcePath": str(hooks_file),
                              "enabled": not (disabled and event == "preToolUse"), "currentHash": f"h{i}"})
        self.stdin = self
        self.stdout = self

    def write(self, line):
        msg = json.loads(line)
        if "id" not in msg:
            return
        if self.rpc_error and msg["method"] == self.rpc_error:
            self.out.append(json.dumps({"id": msg["id"], "error": {"message": "boom"}}) + "\n")
            return
        res = {}
        if msg["method"] == "hooks/list":
            self.lists += 1
            rows = [dict(r) for r in self.rows]
            if self.lists > 1 and self.relist:
                rows = self.relist(rows)
            res = {"data": [{"hooks": rows}]}
        elif msg["method"] == "config/value/write" and self.trust_works:
            key = msg["params"]["keyPath"].split('"')[1]
            for r in self.rows:
                if r["key"] == key:
                    r["trustedHash"] = msg["params"]["value"]
        self.out.append(json.dumps({"id": msg["id"], "result": res}) + "\n")

    def flush(self):
        pass

    def readline(self):
        return self.out.pop(0) if self.out else ""

    def kill(self):
        pass

    def wait(self):
        pass


def _trust_with(monkeypatch, tmp_path, **kw):
    chp = tmp_path / "hooks.json"
    ui.seed_codex_hook(chp, tmp_path, str(tmp_path))
    data = _hooks_file(chp)
    data["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs"}]})
    chp.write_text(json.dumps(data), encoding="utf-8")
    cmds = [(event[0].lower() + event[1:], handler["command"])
            for event, groups in data["hooks"].items()
            for group in groups for handler in group.get("hooks", [])]
    fake = _FakeCodex(chp, cmds, **kw)
    monkeypatch.setattr(ui.shutil, "which", lambda n: "/bin/codex")
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: fake)
    return ui.trust_codex_hook(chp, dest_dir=tmp_path), fake


def test_trust_covers_guard_and_skips_foreign(monkeypatch, tmp_path):
    (ok, note), fake = _trust_with(monkeypatch, tmp_path)
    assert ok, note
    assert len([r for r in fake.rows if r.get("trustedHash")]) == 11
    assert all(r.get("trustedHash") == r["currentHash"] for r in fake.rows if r["command"] != "theirs")
    assert next(r for r in fake.rows if r["command"] == "theirs").get("trustedHash") is None


def test_native_trust_renews_only_changed_owned_hashes(monkeypatch, tmp_path):
    (ok, note), fake = _trust_with(monkeypatch, tmp_path)
    assert ok, note
    writes = []
    original = fake.write

    def write(line):
        msg = json.loads(line)
        if msg.get("method") == "config/value/write":
            writes.append(msg)
        return original(line)

    fake.write = write
    assert ui.trust_codex_hook(tmp_path / "hooks.json", dest_dir=tmp_path)[0] is True
    assert writes == []
    fake.rows[0]["currentHash"] = "owned-script-changed"
    assert ui.trust_codex_hook(tmp_path / "hooks.json", dest_dir=tmp_path)[0] is True
    assert len(writes) == 1 and writes[0]["params"]["value"] == "owned-script-changed"


def _drop(event):
    return lambda rows: [r for r in rows if r["eventName"] != event]


def _blank(field):
    def fn(rows):
        for r in rows:
            r.pop(field, None)
        return rows
    return fn


def _set(field, value):
    def fn(rows):
        for r in rows:
            r[field] = value
        return rows
    return fn


def _trust_status_only(rows):
    for r in rows:
        r.pop("trustedHash", None)
        r["trustStatus"] = "trusted"
    return rows


def _duplicate_guard(rows):
    return rows + [dict(r, key="dup") for r in rows if r["eventName"] == "preToolUse" and r["command"] != "theirs"]


def _rehash(rows):
    for r in rows:
        r["currentHash"] = "changed"
    return rows


@pytest.mark.parametrize("kw", [
    {"disabled": True}, {"trust_works": False}, {"hide_guard": True}, {"hide_session": True},
    {"relist": lambda rows: []},
    {"relist": _drop("sessionStart")},
    {"relist": _drop("preToolUse")},
    {"relist": _blank("trustedHash")},
    {"relist": _blank("currentHash")},
    {"relist": _set("trustedHash", "")},
    {"relist": _set("enabled", None)},
    {"relist": _set("trustedHash", "stale")},
    {"relist": _duplicate_guard},
    {"relist": _rehash},
    {"rpc_error": "hooks/list"},
    {"rpc_error": "config/value/write"},
], ids=lambda v: None)
def test_unverifiable_trust_is_refused(monkeypatch, tmp_path, kw):
    (ok, note), _ = _trust_with(monkeypatch, tmp_path, **kw)
    assert ok is False and note


def test_trust_status_without_trusted_hash_is_accepted_as_real_codex_reports_it(monkeypatch, tmp_path):
    (ok, note), _ = _trust_with(monkeypatch, tmp_path, relist=_trust_status_only)
    assert ok, note


def test_second_discovery_empty_is_not_trusted(monkeypatch, tmp_path):
    (ok, note), _ = _trust_with(monkeypatch, tmp_path, relist=lambda rows: [])
    assert not ok and "did not discover" in note


def test_absent_hashes_on_verification_are_not_trusted(monkeypatch, tmp_path):
    (ok, _), _ = _trust_with(monkeypatch, tmp_path, relist=lambda rows: [
        {k: v for k, v in r.items() if k not in ("currentHash", "trustedHash")} for r in rows])
    assert not ok


def test_hashes_absent_before_write_are_refused(monkeypatch, tmp_path):
    (ok, note), fake = _trust_with(monkeypatch, tmp_path, hide_session=True)
    assert not ok and "sessionstart" in note
    assert all("trustedHash" not in r for r in fake.rows)


def test_trust_refuses_a_hooks_file_without_both_handlers(monkeypatch, tmp_path):
    chp = tmp_path / "hooks.json"
    chp.write_text(json.dumps({"hooks": {"SessionStart": []}}), encoding="utf-8")
    monkeypatch.setattr(ui.shutil, "which", lambda n: "/bin/codex")
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: _FakeCodex(chp, []))
    ok, note = ui.trust_codex_hook(chp)
    assert not ok and "exactly one" in note


@pytest.mark.parametrize("trust_result", [
    (False, "codex not on PATH; hook stays untrusted until reviewed in /hooks"),
    (False, "codex did not discover the pretooluse handler (features.hooks disabled?)"),
    (False, "codex hook trust failed: boom"),
    (False, "codex app-server did not start: nope"),
])
def test_main_every_trust_failure_is_nonzero_and_keeps_doctrine(monkeypatch, tmp_path, trust_result):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf,
                 codex_trust=lambda p: trust_result)
    out = buf.getvalue()
    assert rc != 0 and "governance verification FAILED" in out and "hook wiring/trust verified" not in out
    assert ui.DOCTRINE_BEGIN in (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")


def test_main_reports_governance_verification_failed(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf,
                 hooks_path=tmp_path / "h.json", codex_hooks=tmp_path / "c.json",
                 codex_trust=lambda p: (False, "PreToolUse is disabled"))
    out = buf.getvalue()
    assert rc != 0 and "governance verification FAILED" in out and "governance verified" not in out


def test_main_success_says_wiring_verified_and_enforcement_unverified(monkeypatch, tmp_path, _engine):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf,
                 codex_trust=lambda p: (True, "trusted 2 hook(s)"))
    out = buf.getvalue()
    assert rc == 0 and "hook wiring/trust verified" in out
    assert "governance verified" not in out and "enforcement UNVERIFIED until the C6" in out


# Split so the publish rename (authoring key -> published key) cannot collapse the two
# registry keys into one: a repeated keyword is a SyntaxError in the published mirror.
_AUTHORING_KEY = "project_" + "claude-klabauter"


def _registry(monkeypatch, tmp_path, **repos):
    ml = tmp_path / "machine-local"
    ml.mkdir(exist_ok=True)
    body = "[repos]\n" + "".join(f'{k} = "{Path(v).as_posix()}"\n' for k, v in repos.items())
    (ml / "registry.local.toml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml))
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)


def _stamped_root(path):
    (path / "coordinator_core").mkdir(parents=True)
    (path / "coordinator_core" / "_engine_stamp").write_text("stamped\n", encoding="utf-8")
    return path


def test_resolver_returns_published_klabauter_never_the_authoring_checkout(monkeypatch, tmp_path):
    klabauter = _stamped_root(tmp_path / "klabauter")
    authoring = _stamped_root(tmp_path / "authoring")
    _registry(monkeypatch, tmp_path, **{"claude_klabauter": klabauter, _AUTHORING_KEY: authoring})
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    root, prov = _REAL_RESOLVE()
    assert root == str(klabauter) and "repos.claude_klabauter" in prov


@pytest.mark.parametrize("repos", ["none", "unstamped", "authoring-only"])
def test_resolver_refuses_naming_the_key_when_klabauter_is_not_published(monkeypatch, tmp_path, repos):
    authoring = _stamped_root(tmp_path / "authoring")
    kwargs = {"none": {}, "authoring-only": {_AUTHORING_KEY: authoring},
              "unstamped": {"claude_klabauter": tmp_path}}[repos]
    _registry(monkeypatch, tmp_path, **kwargs)
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    root, prov = _REAL_RESOLVE()
    assert root is None and "repos.claude_klabauter" in prov


def test_default_install_resolves_engine_root_through_settings_home_door(monkeypatch, tmp_path):
    root = tmp_path / "door-root"
    (root / "coordinator_core").mkdir(parents=True)
    (root / "coordinator_core" / "_engine_stamp").write_text("stamped\n", encoding="utf-8")
    (root / "coordinator_core" / "session").mkdir()
    (root / "coordinator_core" / "session" / "external_registry.py").write_text("# capability fixture", encoding="utf-8")
    (root / "coordinator_core" / "host_hooks").mkdir(exist_ok=True)
    (root / "coordinator_core" / "host_hooks" / "session_context.py").write_text("# capability fixture", encoding="utf-8")
    (root / "coordinator_core" / "host_hooks" / "codex_lifecycle.py").write_text("# capability fixture", encoding="utf-8")
    (root / "coordinator_core" / "codex_status").mkdir(exist_ok=True)
    (root / "coordinator_core" / "codex_status" / "hook.py").write_text("# capability fixture", encoding="utf-8")
    monkeypatch.setattr(ui, "resolve_engine_root", _REAL_RESOLVE)
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    _registry(monkeypatch, tmp_path, claude_klabauter=root)
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 0 and f"governance engine root: {root} (published klabauter (repos.claude_klabauter))" in out
    assert "governance DEGRADED" not in out
    codex = _hooks_file(home / ".codex" / "hooks.json")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    agy = _hooks_file(home / ".gemini" / "config" / "hooks.json")[ui.AGY_HOOK_KEY]["PreToolUse"][0]["hooks"][0]["command"]
    assert f"--engine-root {_fwd(root)}" in _shown(codex) and f"--engine-root {_fwd(root)}" in _shown(agy)


def test_degraded_line_names_the_resolver_failure_reason(monkeypatch, tmp_path):
    bin_dir = tmp_path / "fakeplugin" / "bin"
    shim_dir = tmp_path / "fakeplugin" / "lib" / "resolve-claude-klabauter"
    shim_dir.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    (shim_dir / "_resolve_claude_klabauter.py").write_text(
        "def _resolve_published_engine(ml):\n    return None\n"
        "def _ml_dir():\n    raise RuntimeError('no engine root\\nfix it')\n", encoding="utf-8")
    monkeypatch.setattr(ui, "resolve_engine_root", _REAL_RESOLVE)
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    monkeypatch.delenv("COORDINATOR_ENGINE_ROOT", raising=False)
    # _seed() caches the doctrine-seed module from _BIN_DIR; load the real one before _BIN_DIR points at
    # the fake plugin, or this test passes only when an earlier test happened to load it.
    ui._seed()
    monkeypatch.setattr(ui, "_BIN_DIR", bin_dir)
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 7 and "governance DEGRADED: engine root unresolved (resolver failed: no engine root)" in out
    cmd = _hooks_file(home / ".codex" / "hooks.json")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--engine-root" not in cmd


class _Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _scheduler(task_exists=True):
    calls = []

    def run(*args):
        calls.append(args)
        return _Proc(0 if args[0] != "/Query" or task_exists else 1)

    return run, calls


def test_macos_login_agent_reruns_autolink_on_an_interval_without_keepalive(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    dest, agents = tmp_path / "unimatrix", tmp_path / "LaunchAgents"
    assert ui.register_login_agent(dest, agents)[0] == "created"
    [plist] = list(agents.iterdir())
    import plistlib
    spec = plistlib.loads(plist.read_bytes())
    assert plist.name == f"{ui.LOGIN_AGENT_LABEL}.plist" and spec["Label"] == ui.LOGIN_AGENT_LABEL
    assert spec["RunAtLoad"] is True
    assert spec["StartInterval"] == 60 and spec["AbandonProcessGroup"] is True
    assert "KeepAlive" not in spec
    assert spec["ProgramArguments"] == [sys.executable, str(dest / ui.AUTOLINK_SCRIPT)]
    assert spec["ProgramArguments"][1].startswith(str(tmp_path))


def test_login_agent_runs_the_unimatrix_python_pin_whichever_python_reinstalls(monkeypatch, tmp_path):
    """A reinstall from a different interpreter leaves a pinned plist byte-identical: no reload, no grant loss."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ui, "_pin_value", lambda: "/pinned/python3")  # abs-path-ok: synthetic pin, never opened
    dest, agents = tmp_path / "unimatrix", tmp_path / "LaunchAgents"
    assert ui.register_login_agent(dest, agents)[0] == "created"
    import plistlib
    [plist] = list(agents.iterdir())
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == "/pinned/python3"
    monkeypatch.setattr(sys, "executable", "/other/python3.15")  # abs-path-ok: synthetic, never opened
    assert ui.register_login_agent(dest, agents)[0] == "present"


def test_first_install_pins_the_interpreter_the_login_agent_already_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    granted = tmp_path / "granted-python3"
    granted.write_text("")
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(ui, "_pin_value", lambda: str(granted))
    ui.register_login_agent(tmp_path / "unimatrix", agents)
    written = []
    monkeypatch.setattr(ui, "_pin_raw", lambda: None)
    monkeypatch.setattr(ui, "_write_pin", lambda interp: written.append(interp) or True)
    monkeypatch.setattr(sys, "executable", "/other/python3.15")  # abs-path-ok: synthetic, never opened
    assert ui.record_python_pin(agents) == str(granted) and written == [str(granted)]


def test_first_install_without_a_login_agent_pins_the_installing_python(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    written = []
    monkeypatch.setattr(ui, "_pin_raw", lambda: None)
    monkeypatch.setattr(ui, "_write_pin", lambda interp: written.append(interp) or True)
    assert ui.record_python_pin(tmp_path / "LaunchAgents") == sys.executable and written == [sys.executable]


def test_a_live_pin_is_never_overwritten(monkeypatch, tmp_path):
    live = tmp_path / "live-python3"
    live.write_text("")
    monkeypatch.setattr(ui, "_pin_raw", lambda: str(live))
    assert ui.record_python_pin(tmp_path) is None  # autouse _write_pin fails the test on any write


def test_a_pin_whose_file_is_absent_is_repinned_to_the_installing_python(monkeypatch, tmp_path):
    written = []
    monkeypatch.setattr(ui, "_pin_raw", lambda: str(tmp_path / "gone-python3"))
    monkeypatch.setattr(ui, "_write_pin", lambda interp: written.append(interp) or True)
    assert ui.record_python_pin(tmp_path) == sys.executable and written == [sys.executable]


def test_linux_login_unit_carries_no_interval_or_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    text = ui._login_agent_text(tmp_path / "unimatrix").decode("utf-8")
    assert "Type=oneshot" in text
    assert not any(k in text for k in ("StartInterval", "Restart=", "OnUnitActiveSec"))


def test_login_agent_reinstall_leaves_one_registration_and_heals_a_stale_one(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    agents = tmp_path / "LaunchAgents"
    ui.register_login_agent(tmp_path / "a", agents)
    assert ui.register_login_agent(tmp_path / "a", agents)[0] == "present"
    assert ui.register_login_agent(tmp_path / "b", agents)[0] == "updated"
    [plist] = list(agents.iterdir())
    assert str(tmp_path / "b") in plist.read_text(encoding="utf-8", errors="replace")


def test_login_agent_uninstall_removes_it_and_tolerates_absence(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    agents = tmp_path / "LaunchAgents"
    assert ui.unregister_login_agent(agents) == "absent"
    ui.register_login_agent(tmp_path / "a", agents)
    assert ui.unregister_login_agent(agents) == "removed"
    assert list(agents.iterdir()) == [] and ui.unregister_login_agent(agents) == "absent"


def test_test_home_refuses_the_real_hkcu_run_write_and_touches_no_registry(monkeypatch, tmp_path):
    import types

    touched = []

    class _Real(types.ModuleType):
        def __getattr__(self, name):
            touched.append(name)
            raise AssertionError(name)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ui, "_winreg", lambda: _Real("winreg"))
    with pytest.raises(RuntimeError, match="test home refuses real run_key"):
        ui.register_login_agent(tmp_path / "u", schtasks=_scheduler(task_exists=False)[0])
    with pytest.raises(RuntimeError, match="test home refuses real run_key"):
        ui.unregister_login_agent(schtasks=_scheduler(task_exists=False)[0])
    assert touched == []


def test_test_home_refuses_the_default_login_agent_dir_under_the_real_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    real_dir = ui.login_agent_dir()
    before = real_dir.exists()
    with pytest.raises(RuntimeError, match="test home refuses real login_agent_dir"):
        ui.register_login_agent(tmp_path / "u")
    assert real_dir.exists() == before


def test_a_default_login_agent_dir_under_the_test_home_still_resolves(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    home = Path(os.environ["COORDINATOR_SETTINGS_HOME"])
    monkeypatch.setattr(ui.Path, "home", classmethod(lambda cls: home))
    assert ui.register_login_agent(tmp_path / "u")[0] == "created"
    assert (home / "Library" / "LaunchAgents" / f"{ui.LOGIN_AGENT_LABEL}.plist").is_file()


def test_login_agent_guard_is_off_without_the_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("UNIMATRIX_TEST_HOME")
    monkeypatch.setattr(ui.Path, "home", classmethod(lambda cls: tmp_path / "h"))
    assert ui.register_login_agent(tmp_path / "u")[0] == "created"


def test_linux_login_agent_is_a_oneshot_user_unit_enabled_by_symlink(monkeypatch, tmp_path):
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(tmp_path)
    except OSError:
        pytest.skip("this host cannot create symlinks (Windows without Developer Mode); the Linux unit link needs one")
    monkeypatch.setattr(sys, "platform", "linux")
    units = tmp_path / "systemd" / "user"
    assert ui.register_login_agent(tmp_path / "u", units)[0] == "created"
    unit = (units / ui.LOGIN_UNIT).read_text(encoding="utf-8")
    assert "Type=oneshot" in unit and str(tmp_path / "u" / ui.AUTOLINK_SCRIPT) in unit
    assert "Restart" not in unit and "Timer" not in unit
    link = units / "default.target.wants" / ui.LOGIN_UNIT
    assert link.is_symlink() and link.resolve() == (units / ui.LOGIN_UNIT).resolve()
    timer_link = units / "timers.target.wants" / ui.LOGIN_TIMER
    assert timer_link.is_symlink() and timer_link.resolve() == (units / ui.LOGIN_TIMER).resolve()
    assert ui.register_login_agent(tmp_path / "u", units)[0] == "present"
    assert ui.unregister_login_agent(units) == "removed"
    assert not (units / ui.LOGIN_UNIT).exists() and not link.is_symlink()
    assert not (units / ui.LOGIN_TIMER).exists() and not timer_link.is_symlink()


def test_linux_login_unit_keeps_spawned_supervisors_alive_and_relinks(monkeypatch, tmp_path):
    # Symlink-free, so it runs on a Windows host too. KillMode=process: the default control-group mode
    # reaps the supervisors autolink spawned when the oneshot exits (Gipsy, live).
    monkeypatch.setattr(sys, "platform", "linux")
    unit = ui._login_agent_text(tmp_path / "u").decode("utf-8")
    assert "Type=oneshot" in unit and "KillMode=process" in unit
    assert "Wants=network-online.target" in unit and "After=network-online.target" in unit
    timer = ui._login_timer_text().decode("utf-8")
    assert f"OnUnitInactiveSec={ui.LOGIN_AGENT_INTERVAL_S}" in timer and f"Unit={ui.LOGIN_UNIT}" in timer
    assert "OnBootSec=30" in timer and "WantedBy=timers.target" in timer


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


@pytest.fixture
def winreg_fake(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setattr(ui, "_winreg", lambda: fake)
    monkeypatch.setattr(sys, "platform", "win32")
    return fake


def test_windows_login_agent_is_an_hkcu_run_value_overwritten_in_place(winreg_fake, tmp_path):
    run, calls = _scheduler(task_exists=False)
    assert ui.register_login_agent(tmp_path / "u", schtasks=run)[0] == "registered"
    assert ui.register_login_agent(tmp_path / "u", schtasks=run)[0] == "registered"
    assert winreg_fake.opened[0] == ("HKCU", ui.RUN_KEY)
    assert list(winreg_fake.values) == [ui.LOGIN_TASK_NAME]
    cmd = winreg_fake.values[ui.LOGIN_TASK_NAME]
    assert ui.AUTOLINK_SCRIPT in cmd and str(tmp_path / "u") in cmd
    assert not any(c[0] == "/Create" for c in calls)


def test_windows_run_command_prefers_pythonw_beside_the_interpreter(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_text("", encoding="utf-8")
    (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    cmd = ui._windows_run_command(tmp_path / "u")
    assert "pythonw.exe" in cmd and ui.AUTOLINK_SCRIPT in cmd
    (tmp_path / "pythonw.exe").unlink()
    assert "pythonw.exe" not in ui._windows_run_command(tmp_path / "u")


def test_windows_registration_migrates_an_existing_scheduled_task(winreg_fake, tmp_path):
    run, calls = _scheduler(task_exists=True)
    assert ui.register_login_agent(tmp_path / "u", schtasks=run)[0] == "registered"
    assert calls[-1] == ("/Delete", "/TN", ui.LOGIN_TASK_NAME, "/F")
    assert ui.LOGIN_TASK_NAME in winreg_fake.values


def test_windows_login_agent_uninstall_deletes_value_and_task_and_tolerates_absence(winreg_fake, tmp_path):
    run, calls = _scheduler(task_exists=True)
    ui.register_login_agent(tmp_path, schtasks=_scheduler(task_exists=False)[0])
    assert ui.unregister_login_agent(schtasks=run) == "removed"
    assert winreg_fake.values == {} and calls[-1] == ("/Delete", "/TN", ui.LOGIN_TASK_NAME, "/F")
    run, calls = _scheduler(task_exists=False)
    assert ui.unregister_login_agent(schtasks=run) == "absent"
    ui.register_login_agent(tmp_path, schtasks=run)
    assert ui.unregister_login_agent(schtasks=run) == "removed" and winreg_fake.values == {}


def test_windows_registry_failure_is_a_skip_not_an_error(winreg_fake, tmp_path):
    winreg_fake.fail = True
    state, where = ui.register_login_agent(tmp_path, schtasks=_scheduler(task_exists=False)[0])
    assert state == "skipped" and "HKCU Run" in where and winreg_fake.values == {}


def test_main_install_registers_one_login_agent_and_uninstall_removes_it(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    hooks = tmp_path / "g" / "hooks.json"
    rc, out = _run(["--install"], "2.1.316", tmp_path / "d", hooks)
    assert rc == 0 and "login agent created" in out
    assert _run(["--install"], "2.1.316", tmp_path / "d", hooks)[1].count("login agent present") == 1
    [plist] = list((tmp_path / "g" / "login-agent").iterdir())
    assert plist.name == f"{ui.LOGIN_AGENT_LABEL}.plist"
    buf = io.StringIO()
    assert ui.main(["--uninstall"], out=buf, hooks_path=hooks) == 0
    assert "login agent removed" in buf.getvalue() and not plist.exists()


def test_main_default_install_writes_the_agent_under_home_and_never_the_real_one(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 0 and (home / "Library" / "LaunchAgents" / f"{ui.LOGIN_AGENT_LABEL}.plist").is_file()


LEGACY = "com.coordinator.unimatrix-autolink"


def test_a_failed_bootout_is_not_reported_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")

    def lc(*args):
        return subprocess.CompletedProcess(["launchctl", *args], 0 if args[0] == "print" else 5, "", "")

    assert ui.remove_legacy_login_agents(tmp_path / "LaunchAgents", lc, uid=7) == []


def test_the_doctors_label_pair_is_the_installers_current_and_legacy_labels():
    spec = importlib.util.spec_from_file_location("unimatrix_doctor_label_sync", Path(ui.__file__).parent / "unimatrix-doctor.py")
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    assert dr.AUTOLINK_LABELS == (ui.LOGIN_AGENT_LABEL, *ui.LEGACY_LOGIN_AGENT_LABELS)


def _lstart(epoch):
    t = __import__("time").localtime(epoch)
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"Mon {months[t.tm_mon - 1]} {t.tm_mday:2d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} {t.tm_year}"


def _ps_row(pid, ppid, epoch, dest, script="unimatrix-relay.py"):
    return f"{pid} {ppid} {_lstart(epoch)} /usr/bin/python3 {dest / script}"


def test_reap_kills_orphaned_and_replaced_code_relays_and_keeps_the_live_one(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    dest = tmp_path / "unimatrix"
    dest.mkdir()
    (dest / "unimatrix-relay.py").write_text("# new\n", encoding="utf-8")
    stamp = (dest / "unimatrix-relay.py").stat().st_ctime
    ps = "\n".join([
        _ps_row(201, 1, stamp + 50, dest),
        _ps_row(202, 100, stamp - 5000, dest),
        _ps_row(203, 100, stamp + 50, dest),
        f"204 100 {_lstart(stamp - 5000)} /usr/sbin/sshd-session -R",
    ])
    monkeypatch.setattr(ui, "_pid_alive", lambda pid: pid == 100)
    killed = []
    stale, mismatched, failed = ui.reap_stale_relays(dest, lambda: ps, killed.append)
    assert (stale, mismatched, failed) == ([201], [202], [])
    assert sorted(killed) == [201, 202]


def test_reap_reports_a_pid_it_cannot_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    dest = tmp_path / "u"
    dest.mkdir()
    (dest / "unimatrix-relay.py").write_text("#\n", encoding="utf-8")
    ps = _ps_row(301, 1, 1_000_000_000, dest)

    def deny(pid):
        raise PermissionError(pid)

    assert ui.reap_stale_relays(dest, lambda: ps, deny) == ([301], [], [301])


def test_reap_is_skipped_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert ui.reap_stale_relays(tmp_path, lambda: pytest.fail("ps ran"), lambda p: pytest.fail("kill ran")) == ([], [], [])


def test_an_unchanged_script_is_not_recopied_so_its_ctime_marks_the_last_code_change(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    src, dest = tmp_path / "src", tmp_path / "dest"
    ui.install_scripts(src, dest)
    copies = []
    real = ui.shutil.copy2
    monkeypatch.setattr(ui.shutil, "copy2", lambda a, b: (copies.append(b), real(a, b)))
    ui.install_scripts(src, dest)
    assert copies == []
    (src / "unimatrix-relay.py").write_text("# changed\n", encoding="utf-8")
    ui.install_scripts(src, dest)
    assert [Path(c).name for c in copies] == ["unimatrix-relay.py"]


def test_forced_command_interpreter_honours_the_pin_then_homebrew_then_console(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(brew),))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ui.forced_command_interpreter("/pinned/python3") == "/pinned/python3"  # abs-path-ok: synthetic pin, never opened
    assert ui.forced_command_interpreter(None) == str(brew)
    monkeypatch.setattr(ui, "_pin_value", lambda: "/read/python3")  # abs-path-ok: synthetic pin, never opened
    assert ui.forced_command_interpreter() == "/read/python3"  # abs-path-ok: synthetic pin, never opened


def test_main_windows_install_registers_the_run_value_and_migrates_the_task(winreg_fake, monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    run, calls = _scheduler()
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf,
                 hooks_path=tmp_path / "g" / "hooks.json", schtasks=run)
    assert rc == 0 and "login agent registered" in buf.getvalue() and calls[0][0] == "/Query"
    assert ui.LOGIN_TASK_NAME in winreg_fake.values


def _pair_home(monkeypatch, tmp_path, reader=True):
    home = tmp_path / "sh"
    (home / "bin").mkdir(parents=True)
    if reader:
        (home / "bin" / "_machine_local.py").write_text("# stub\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def test_recorded_pair_keys_refuses_a_registry_read_exiting_other_than_0_or_1(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    done = lambda rc, err="": type("R", (), {"returncode": rc, "stdout": "", "stderr": err})()
    with pytest.raises(RuntimeError, match="unimatrix.far_boxes failed: boom"):
        ui.recorded_pair_keys(run=lambda argv, **kw: done(2, "boom"))
    assert ui.recorded_pair_keys(run=lambda argv, **kw: done(1)) == {}


def test_authorized_keys_line_key_body_with_posix_and_windows_interpreters():
    posix = ui.authorized_keys_line("/home/me/unimatrix", "/usr/bin/python3", "KB")
    win = ui.authorized_keys_line(r"\Users\me\unimatrix", r"\Python312\python.exe", "KB")
    assert posix.endswith(" ssh-ed25519 KB unimatrix") and win.endswith(" ssh-ed25519 KB unimatrix")
    assert "/usr/bin/python3 " in posix and "python.exe " in win
    assert "<key-body>" in ui.authorized_keys_line("/d", "/p")


def _engine_tree_and_plugin(monkeypatch, tmp_path):
    engine = tmp_path / "engine-tree" / "coordinator"
    engine.mkdir(parents=True)
    monkeypatch.setattr(ui, "PLUGIN_ROOT", engine)
    monkeypatch.setattr(ui, "SKILLS_ROOT", engine / "skills")
    plugin = tmp_path / "doe" / "coordinator"
    doc = plugin.parent / "global-doctrine"
    (doc / "rules").mkdir(parents=True)
    (doc / "CLAUDE.md").write_text("# doctrine\n", encoding="utf-8")
    (doc / "rules" / "rules.md").write_text("- a rule\n", encoding="utf-8")
    (plugin / "skills" / "one").mkdir(parents=True)
    (plugin / "skills" / "one" / "SKILL.md").write_text("# one\n", encoding="utf-8")
    return plugin


def test_plugin_root_seeds_doctrine_and_skills_from_outside_the_engine_tree(monkeypatch, tmp_path):
    plugin = _engine_tree_and_plugin(monkeypatch, tmp_path)
    home, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex,agy", "--plugin-root", str(plugin)))
    assert rc == 0 and "WARN" not in out
    for rules in (home / ".codex" / "AGENTS.md", home / ".gemini" / "GEMINI.md"):
        assert "<!-- BEGIN coordinator-doctrine" in rules.read_text(encoding="utf-8")
    assert not (home / ".gemini" / "config" / "skills.json").exists()
    assert any((home / ".agents" / "skills").glob("*.coordinator-managed"))


def test_plugin_root_doctrine_found_in_the_root_itself(monkeypatch, tmp_path):
    _engine_tree_and_plugin(monkeypatch, tmp_path)
    plugin = tmp_path / "flat"
    (plugin / "global-doctrine").mkdir(parents=True)
    (plugin / "global-doctrine" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    assert ui.doctrine_root(plugin) == plugin
    assert ui.doctrine_root(tmp_path / "elsewhere" / "none") is None


def test_without_plugin_root_the_engine_tree_warns_and_skips(monkeypatch, tmp_path):
    _engine_tree_and_plugin(monkeypatch, tmp_path)
    home, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex,agy"))
    assert rc == 7 and "WARN: codex coordinator doctrine not installed" in out
    assert "PARTIAL INSTALL: not installed: codex coordinator doctrine, agy coordinator doctrine" in out
    assert not (home / ".codex" / "AGENTS.md").exists() or "coordinator-doctrine" not in (
        home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")


def test_plugin_root_uninstall_removes_what_install_seeded(monkeypatch, tmp_path):
    plugin = _engine_tree_and_plugin(monkeypatch, tmp_path)
    home, rc, _ = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex,agy", "--plugin-root", str(plugin)))
    assert rc == 0 and (home / ".gemini" / "config" / "plugins.json").exists()
    buf = io.StringIO()
    assert ui.main(["--uninstall", "--hosts", "codex,agy", "--plugin-root", str(plugin)], out=buf) == 0
    assert not (home / ".gemini" / "config" / "skills.json").exists()
    assert not (home / ".gemini" / "GEMINI.md").exists()
    assert not list((home / ".agents" / "skills").glob("*"))


def test_flat_checkout_templates_doctrine_seeds_codex_and_agy(monkeypatch, tmp_path):
    flat = tmp_path / "flat"
    (flat / "templates" / "global-doctrine" / "rules").mkdir(parents=True)
    (flat / "templates" / "global-doctrine" / "CLAUDE.md").write_text("# flat\n", encoding="utf-8")
    (flat / "templates" / "global-doctrine" / "rules" / "rules.md").write_text("- r\n", encoding="utf-8")
    (flat / "coordinator").mkdir()
    (flat / "coordinator" / "skills" / "required").mkdir(parents=True)
    (flat / "coordinator" / "skills" / "required" / "SKILL.md").write_text("# required\n")
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "none" / "coordinator")
    assert ui.doctrine_root(flat / "coordinator") == flat / "templates"
    assert ui.doctrine_root(flat) == flat / "templates"
    home, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex,agy", "--plugin-root", str(flat / "coordinator")))
    assert rc == 0 and "PARTIAL" not in out
    for rules in (home / ".codex" / "AGENTS.md", home / ".gemini" / "GEMINI.md"):
        assert "# flat" in rules.read_text(encoding="utf-8")


def test_missing_peer_doctrine_template_still_writes_send_rule_and_flags_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "PEER_DOCTRINE_TEMPLATE", tmp_path / "absent-peer-doctrine.md")
    home, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex,agy"))
    assert rc == 7
    assert "PARTIAL INSTALL: not installed: codex peer doctrine, agy peer doctrine" in out
    assert "codex send rule" in out and "codex send rule not installed" not in out
    assert (home / ".codex" / "rules" / "unimatrix.rules").is_file()
    assert "coordinator-doctrine" in (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")


def test_unwritable_send_rule_is_named_in_the_partial_line(monkeypatch, tmp_path):
    def boom(path, text):
        raise OSError("read-only rules dir")
    monkeypatch.setattr(ui, "seed_file", boom)
    _, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex"))
    assert rc == 7 and "PARTIAL INSTALL: not installed: codex send rule" in out


def test_complete_codex_and_agy_install_prints_no_partial_line(monkeypatch, tmp_path):
    _, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex,agy"))
    assert rc == 0 and "PARTIAL INSTALL" not in out


def test_codex_only_install_skips_claude_gate_even_with_claude_on_path(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(ui.shutil, "which", lambda name: "/usr/bin/claude")

    def probe():
        raise AssertionError("the Claude gate must not run for --hosts codex")
    monkeypatch.setattr(ui, "probe_claude_version", probe)
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "codex"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "gate skipped" in buf.getvalue()


def test_agy_config_seeded_empty_0600_only_when_absent(monkeypatch, tmp_path):
    home, rc, out = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "agy"))
    cfg = home / ".gemini" / "config" / "config.json"
    assert rc == 0 and cfg.read_text(encoding="utf-8").strip() == "{}"
    if sys.platform != "win32":
        assert (cfg.stat().st_mode & 0o777) == 0o600
    cfg.write_text('{"mine": 1}', encoding="utf-8")
    rc = ui.main(["--install", "--hosts", "agy"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=io.StringIO())
    assert rc == 0 and cfg.read_text(encoding="utf-8") == '{"mine": 1}'


def test_seed_agy_plugin_is_idempotent_and_preserves_other_entries(tmp_path):
    pj = tmp_path / "home" / ".gemini" / "config" / "plugins.json"
    tree = tmp_path / "dest" / "agy-plugin"
    tree.mkdir(parents=True)
    assert ui.seed_agy_plugin(pj, tree) == "created"
    assert ui.seed_agy_plugin(pj, tree) == "present"
    data = json.loads(pj.read_text(encoding="utf-8"))
    assert data["entries"] == [{"path": str(tree.parent), "include_only": ["agy-plugin"]}]
    mine = tmp_path / "mine"
    mine.mkdir()
    data["entries"].insert(0, {"path": str(mine)})
    data["inherits"] = [{"path": "/shared.json"}]
    pj.write_text(json.dumps(data), encoding="utf-8")
    assert ui.seed_agy_plugin(pj, tree) == "present"
    assert ui.seed_agy_plugin(pj, tmp_path / "other" / "agy-plugin") == "added"
    after = json.loads(pj.read_text(encoding="utf-8"))
    assert after["entries"][0] == {"path": str(mine)} and after["inherits"] == [{"path": "/shared.json"}]
    assert len(after["entries"]) == 3
    assert ui.unseed_agy_plugin(pj, tree) == "removed"
    assert ui.unseed_agy_plugin(pj, tree) == "absent"
    assert len(json.loads(pj.read_text(encoding="utf-8"))["entries"]) == 2


def test_seed_agy_plugin_refuses_a_corrupt_file_untouched(tmp_path):
    pj = tmp_path / "plugins.json"
    pj.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        ui.seed_agy_plugin(pj, tmp_path / "agy-plugin")
    assert pj.read_text(encoding="utf-8") == "{not json"


def test_default_agy_install_registers_plugin_in_plugins_json(monkeypatch, tmp_path):
    plugin = _engine_tree_and_plugin(monkeypatch, tmp_path)
    tree = tmp_path / "dest" / "agy-plugin"
    argv = ("--install", "--hosts", "agy", "--plugin-root", str(plugin))
    home, rc, out = _default_install(monkeypatch, tmp_path, argv)
    pj = home / ".gemini" / "config" / "plugins.json"
    assert tree.is_dir(), out
    entries = json.loads(pj.read_text(encoding="utf-8"))["entries"]
    assert entries == [{"path": str(tree.parent), "include_only": ["agy-plugin"]}]
    ui.main(list(argv), version_text="2.1.316", dest_dir=tmp_path / "dest", out=io.StringIO())
    assert json.loads(pj.read_text(encoding="utf-8"))["entries"] == entries
    assert ui.main(["--uninstall", "--hosts", "agy"], dest_dir=tmp_path / "dest", out=io.StringIO()) == 0
    assert json.loads(pj.read_text(encoding="utf-8"))["entries"] == []


def test_host_map_agy_expands_plugin_root_not_plugin_dir():
    cfg = json.loads((Path(ui.__file__).parent / "unimatrix-host-map.json").read_text(encoding="utf-8"))
    assert cfg["agy"]["plugin_root_token"] == "${PLUGIN_ROOT}"


def test_hosts_writes_only_named_host_paths(monkeypatch, tmp_path):
    home, rc, _ = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex"))
    assert rc == 0 and (home / ".codex").exists()
    assert not (home / ".gemini").exists()
    home2 = tmp_path / "home2"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home2))
    rc = ui.main(["--install", "--hosts", "agy"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=io.StringIO())
    assert rc == 0 and (home2 / ".gemini").exists()
    assert not (home2 / ".codex").exists() and not (home2 / ".agents").exists()


def test_codex_lifecycle_hooks_have_short_timeout_and_keep_transport(tmp_path):
    events = ui.codex_hook_events(tmp_path, tmp_path / "published engine")
    start = events["SessionStart"]["hooks"]
    assert len(start) == 2
    assert ui.CODEX_SESSIONSTART_SCRIPT in start[0]["command"]
    assert ui.EXTERNAL_SESSION_SCRIPT in start[1]["command"]
    assert "--event SessionStart" in _shown(start[1]["command"])
    assert "--engine-root" in _shown(start[0]["command"])
    assert _fwd(tmp_path / "published engine") in _shown(start[0]["command"])
    assert "--engine-root" not in _shown(ui.codex_hook_events(tmp_path)["SessionStart"]["hooks"][0]["command"])
    end = events["SessionEnd"]["hooks"][0]
    assert "--event SessionEnd" in _shown(end["command"])
    assert end["timeout"] <= 3 and start[1]["timeout"] <= 3
    assert _fwd(tmp_path / "published engine") in _shown(end["command"])


def test_codex_install_and_remove_preserve_foreign_handlers_inside_owned_group(tmp_path):
    hp = tmp_path / "hooks.json"
    foreign = {"type":"command", "command":"foreign-inside-group"}
    old = ui.codex_hook_events(tmp_path)["SessionStart"]
    old["hooks"].append(foreign)
    hp.write_text(json.dumps({"hooks":{"SessionStart":[old]}}), encoding="utf-8")
    ui.seed_codex_hook(hp, tmp_path)
    data = _hooks_file(hp)
    assert data["hooks"]["SessionStart"][0]["hooks"] == [foreign]
    assert len(ui._codex_expected_handlers(hp, tmp_path)) == 11
    assert ui.seed_codex_hook(hp, tmp_path) == "present"
    ui.unseed_codex_hook(hp, tmp_path)
    assert _hooks_file(hp) == {"hooks":{"SessionStart":[{"hooks":[foreign]}]}}


def test_duplicate_owned_lifecycle_discovery_is_refused(monkeypatch, tmp_path):
    def duplicate(rows):
        return rows + [dict(row, key="duplicate") for row in rows
                       if "coordinator-external-session.py" in row["command"] and row["eventName"] == "sessionStart"]
    (ok, note), _ = _trust_with(monkeypatch, tmp_path, relist=duplicate)
    assert not ok and "duplicated" in note


def test_missing_session_end_discovery_is_refused(monkeypatch, tmp_path):
    (ok, note), _ = _trust_with(monkeypatch, tmp_path, relist=_drop("sessionEnd"))
    assert not ok and "sessionend" in note


LAUNCHER_CPU_BUDGET_S = 0.5
LAUNCHER_HANG_GUARD_S = 60


def _run_for_cpu_seconds(argv, stdin_text):
    """Run `argv` to completion; return (CompletedProcess, the child's kernel+user CPU seconds).

    Cost is process time, never wall clock: wall clock measures peer load, and a loaded box
    stretches a 100ms launcher past any fixed wall deadline. The wall timeout is only a hang guard."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, creationflags=flags)
        out, err = popen.communicate(stdin_text, timeout=LAUNCHER_HANG_GUARD_S)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        times = [wintypes.FILETIME() for _ in range(4)]
        assert kernel32.GetProcessTimes(wintypes.HANDLE(int(popen._handle)), *map(ctypes.byref, times))
        cpu_s = sum((t.dwHighDateTime << 32) | t.dwLowDateTime for t in times[2:]) / 1e7
    else:
        import resource
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = popen.communicate(stdin_text, timeout=LAUNCHER_HANG_GUARD_S)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_s = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return subprocess.CompletedProcess(argv, popen.returncode, out, err), cpu_s


@pytest.mark.spawns_process
@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd", "SubagentStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop", "PreCompact", "PostCompact"])
def test_external_lifecycle_launcher_calls_published_engine_in_process(tmp_path, event):
    engine = tmp_path / "published engine"
    package = engine / "coordinator_core" / "session"
    package.mkdir(parents=True)
    core = package.parent
    (core / "_engine_stamp").write_text("test-stamp", encoding="utf-8")
    (core / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "external_registry.py").write_text(
        'import pathlib\ndef register_current(payload):\n pathlib.Path(__file__).with_name("registered").write_text(payload["session_id"])\ndef unregister(sid, payload=None):\n pathlib.Path(__file__).with_name("ended").write_text(sid)\n', encoding="utf-8")
    hooks = core / "host_hooks"
    hooks.mkdir()
    (hooks / "__init__.py").write_text("", encoding="utf-8")
    (hooks / "session_context.py").write_text('def context(payload, env):\n return {"event":payload["hook_event_name"]}\n', encoding="utf-8")
    (hooks / "codex_lifecycle.py").write_text('def handle(payload, env):\n return {"event":payload["hook_event_name"]}\n', encoding="utf-8")
    script = Path(ui.__file__).with_name(ui.EXTERNAL_SESSION_SCRIPT)
    proc, cpu_s = _run_for_cpu_seconds(
        [sys.executable, str(script), "--engine-root", str(engine), "--event", event],
        json.dumps({"session_id":"parent-session", **({"agent_id":"child"} if event == "SubagentStart" else {})}))
    assert proc.returncode == 0, proc.stderr
    assert cpu_s < LAUNCHER_CPU_BUDGET_S, f"{event} launcher cost {cpu_s:.3f}s process time"
    assert json.loads(proc.stdout) == ({} if event == "SessionEnd" else {"event":event})
    assert (package / "registered").exists() is (event == "SessionStart")
    assert (package / "ended").exists() is (event == "SessionEnd")


def test_stale_published_engine_keeps_transport_but_reports_partial_lifecycle(monkeypatch, tmp_path, _engine):
    (_engine / "coordinator_core" / "session" / "external_registry.py").unlink()
    home, rc, output = _default_install(monkeypatch, tmp_path)
    assert rc == 7
    assert "session lifecycle DEGRADED" in output and "lacks session.external_registry" in output
    assert "transport hooks remain installed" in output
    assert "hook wiring/trust verified: Codex SessionStart, SessionEnd" not in output
    hooks = _hooks_file(home / ".codex" / "hooks.json")
    assert ui.CODEX_SESSIONSTART_SCRIPT in hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]


@pytest.mark.parametrize("stamp", [None, ""])
def test_external_session_capability_rejects_unstamped_engine(tmp_path, stamp):
    package = tmp_path / "coordinator_core" / "session"
    package.mkdir(parents=True)
    (package / "external_registry.py").write_text("# present", encoding="utf-8")
    if stamp is not None:
        (package.parent / "_engine_stamp").write_text(stamp, encoding="utf-8")
    assert ui.external_session_capability(tmp_path)[0] is False


def test_codex_advisory_events_use_one_bounded_owned_handler(tmp_path):
    events = ui.codex_hook_events(tmp_path, tmp_path / 'engine')
    for event in ('UserPromptSubmit','PostToolUse','Stop','SubagentStop','PreCompact','PostCompact'):
        group = events[event]
        assert len(group['hooks']) == 1
        hook = group['hooks'][0]
        assert hook['timeout'] <= 3
        assert '--event '+event in _shown(hook['command'])
    assert events['PreToolUse']['matcher'] == '.*'


@pytest.mark.parametrize('before', ['', 'model = "gpt-test"\n[tui]\ntheme = "custom"\n', 'tui = { theme = "custom" }\n'])
def test_codex_footer_default_is_idempotent_and_uninstall_owned(tmp_path,before):
    cfg=tmp_path/'config.toml';cfg.write_text(before,encoding='utf-8')
    original=tomllib.loads(before)
    assert ui.seed_codex_status_line(cfg)=='updated'
    parsed=tomllib.loads(cfg.read_text())
    assert parsed['tui']['status_line']==['thread-name']
    assert parsed.get('model')==original.get('model')
    assert parsed['tui'].get('theme')==original.get('tui',{}).get('theme')
    once=cfg.read_bytes()
    assert ui.seed_codex_status_line(cfg)=='present' and cfg.read_bytes()==once
    assert ui.seed_codex_status_line(cfg,uninstall=True)=='restored'
    restored=tomllib.loads(cfg.read_text()); restored_tui=restored.pop('tui',{})
    original_tui=original.pop('tui',{})
    assert restored==original and restored_tui==original_tui


def test_codex_footer_preserves_custom_widgets_and_foreign_toml(tmp_path):
    cfg=tmp_path/'config.toml'
    before='# keep me\nmodel="m"\n[tui]\ntheme="custom" # keep exact\nstatus_line = [\n "git-branch", "current-dir", "five-hour-limit", "model", "reasoning"\n]\n[mcp_servers.foreign]\nurl="http://example"\n'
    cfg.write_text(before)
    assert ui.seed_codex_status_line(cfg)=='updated'
    text=cfg.read_text(); parsed=tomllib.loads(text)
    assert parsed['tui']['status_line']==['git-branch','thread-name','five-hour-limit','model','reasoning']
    assert '# keep me\nmodel="m"' in text and 'theme="custom" # keep exact' in text
    assert '[mcp_servers.foreign]\nurl="http://example"' in text
    cfg.write_text(text.replace('"git-branch"','"weekly-limit"'))
    changed=cfg.read_bytes()
    assert ui.seed_codex_status_line(cfg,uninstall=True)=='retained-customized'
    assert cfg.read_bytes()==changed


def test_codex_footer_does_not_read_fake_tui_header_inside_string(tmp_path):
    cfg=tmp_path/'config.toml'
    cfg.write_text('notice = """\n[tui]\nstatus_line = ["current-dir"]\n"""\n[tui]\nstatus_line=["project-root", "weekly-limit"]\n')
    assert ui.seed_codex_status_line(cfg)=='updated'
    parsed=tomllib.loads(cfg.read_text())
    assert '["current-dir"]' in parsed['notice']
    assert parsed['tui']['status_line']==['thread-name','weekly-limit']


@pytest.mark.parametrize('original,expected',[
    (None,['thread-name']),
    (['git-branch','model','context-remaining'],['thread-name','git-branch','model','context-remaining']),
])
def test_codex_footer_upgrade_drops_natives_we_seeded(tmp_path,original,expected):
    cfg=tmp_path/'config.toml'
    old=['thread-name','model-with-reasoning','context-remaining'] if original is None else ['thread-name']+original+['model-with-reasoning']
    old=list(dict.fromkeys(old))
    cfg.write_text('[tui]\nstatus_line='+json.dumps(old)+'\n')
    cfg.with_name('.coordinator-status-line.json').write_text(json.dumps(
        {'version':1,'present':original is not None,'original':original,'managed':old}))
    assert ui.seed_codex_status_line(cfg)=='updated'
    assert tomllib.loads(cfg.read_text())['tui']['status_line']==expected
    assert ui.seed_codex_status_line(cfg)=='present'
    assert ui.seed_codex_status_line(cfg,uninstall=True)=='restored'


def test_codex_footer_invalid_toml_never_overwritten(tmp_path):
    cfg=tmp_path/'config.toml';cfg.write_text('[tui\n')
    with pytest.raises(ValueError):ui.seed_codex_status_line(cfg)
    assert cfg.read_text()=='[tui\n'


def test_codex_footer_upgrade_retains_custom_additions_without_seeded_duplicates(tmp_path):
    cfg = tmp_path / 'config.toml'
    old = ['thread-name', 'model-with-reasoning', 'context-remaining']
    cfg.write_text('[tui]\nstatus_line=' + json.dumps(old + ['git-branch']) + '\n')
    cfg.with_name('.coordinator-status-line.json').write_text(json.dumps(
        {'version': 1, 'present': False, 'original': None, 'managed': old}))
    assert ui.seed_codex_status_line(cfg) == 'updated'
    assert tomllib.loads(cfg.read_text())['tui']['status_line'] == ['thread-name', 'git-branch']
    assert ui.seed_codex_status_line(cfg) == 'present'


def test_codex_footer_is_automatic_in_install_and_owned_uninstall(monkeypatch,tmp_path):
    _stub_src(monkeypatch,tmp_path)
    home=_home(monkeypatch,tmp_path)
    cfg=home/'.codex'/'config.toml';cfg.parent.mkdir(parents=True)
    before='model="keep"\n[tui]\nstatus_line=["current-dir","git-branch"]\n'
    cfg.write_text(before)
    output=io.StringIO()
    assert ui.main(['--install','--hosts','codex'], version_text='2.1.316',dest_dir=tmp_path/'dest',out=output)==0
    assert 'codex status footer updated' in output.getvalue()
    assert tomllib.loads(cfg.read_text())['tui']['status_line']==['thread-name','git-branch']
    output=io.StringIO()
    assert ui.main(['--uninstall','--hosts','codex'],dest_dir=tmp_path/'dest',out=output)==0
    assert 'codex status footer restored' in output.getvalue()
    assert tomllib.loads(cfg.read_text())==tomllib.loads(before)


@pytest.mark.parametrize('state',[None, {'version':1,'managed':[]}])
def test_codex_footer_malformed_ownership_never_changes_config(tmp_path,state):
    cfg=tmp_path/'config.toml';cfg.write_text('tui.status_line=["current-dir"]\n')
    cfg.with_name('.coordinator-status-line.json').write_text(json.dumps(state))
    before=cfg.read_bytes()
    with pytest.raises(ValueError):ui.seed_codex_status_line(cfg,uninstall=True)
    assert cfg.read_bytes()==before


def test_codex_install_scope_reports_local_tools_with_native_limits(monkeypatch,tmp_path):
    home,rc,output=_default_install(monkeypatch,tmp_path,('--install','--hosts','codex'))
    assert rc==0
    assert 'Codex local-tool PreToolUse (shell, projected edits, MCP, spawn briefs; native coverage limits apply)' in output
    assert 'shell-command PreToolUse only' not in output


def test_file_peer_line_names_the_file_relay_script_and_the_peer_pin():
    line = ui.authorized_keys_line("/home/a/unimatrix", interpreter="/usr/bin/python3", key_body="AAAAk", file_peer="alpha")  # abs-path-ok: synthetic unimatrix path, never opened
    assert line.startswith('restrict,command="/usr/bin/python3 ')
    assert line.endswith('unimatrix-file-relay.py --peer alpha" ssh-ed25519 AAAAk unimatrix')
    plain = ui.authorized_keys_line("/home/a/unimatrix", interpreter="/usr/bin/python3")  # abs-path-ok: synthetic unimatrix path, never opened
    assert "unimatrix-file-relay.py" not in plain and "unimatrix-relay.py" in plain


@pytest.mark.skipif(__import__("shutil").which("ssh-keygen") is None, reason="ssh-keygen required")
def test_file_key_mints_a_separate_key_is_idempotent_and_never_touches_the_relay_key(monkeypatch, tmp_path):
    home = tmp_path / "settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    relay_key = home / ".secrets" / "ssh" / "unimatrix-zero"
    relay_key.parent.mkdir(parents=True)
    relay_key.write_text("relay private key")
    buf = io.StringIO()
    assert ui.main(["--file-key", "--unencrypted-key"], out=buf) == 0
    priv = home / ".secrets" / "ssh" / "unimatrix-file"
    assert priv.is_file() and relay_key.read_text() == "relay private key"
    text = buf.getvalue()
    assert str(priv) in text and "SHA256:" in text and "ssh-ed25519 " in text
    first = priv.read_bytes()
    assert ui.main(["--file-key", "--unencrypted-key"], out=io.StringIO()) == 0 and priv.read_bytes() == first


def test_install_prints_a_separate_file_key_line(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    rc, out = _run(["--install"], "2.1.316", dest, tmp_path / "gemini" / "hooks.json")
    assert rc == 0
    assert [l for l in out.splitlines() if "unimatrix-file-relay.py --peer <box>" in l and "restrict,command=" in l]


def _tui_home(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    _stub_src(monkeypatch, tmp_path)
    return home, home / ".gemini" / "antigravity-cli" / "settings.json", home / ".grok" / "config.toml"


def _tui_main(tmp_path, flag, hosts="agy,grok"):
    buf = io.StringIO()
    rc = ui.main([flag, "--hosts", hosts], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf)
    return rc, buf.getvalue()


def test_tui_status_lines_install_twice_noop_and_uninstall_restores_bytes(monkeypatch, tmp_path):
    _, agy, grok = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    grok.parent.mkdir(parents=True)
    agy_before = b'{\r\n "theme": "dark",\r\n "other": {"k": 1}\r\n}'
    grok_before = b'# mine\r\nmodel = "x"\r\n[mcp.foreign]\r\nurl = "u"'
    agy.write_bytes(agy_before)
    grok.write_bytes(grok_before)
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and "agy status line updated" in out and "grok status line updated" in out
    data = json.loads(agy.read_text(encoding="utf-8"))
    assert data["theme"] == "dark" and data["other"] == {"k": 1}
    assert data["statusLine"]["command"].endswith(" agy")
    parsed = tomllib.loads(grok.read_text(encoding="utf-8"))
    assert parsed["model"] == "x" and parsed["mcp"]["foreign"] == {"url": "u"}
    assert parsed["ui"]["status_line"]["type"] == "command"
    assert parsed["ui"]["status_line"]["command"].endswith(ui.GROK_STATUS_SCRIPT if os.name == "nt" else " grok")
    once = (agy.read_bytes(), grok.read_bytes())
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy status line present" in out and "grok status line present" in out
    assert (agy.read_bytes(), grok.read_bytes()) == once
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and "agy status line restored" in out and "grok status line restored" in out
    assert agy.read_bytes() == agy_before and grok.read_bytes() == grok_before
    assert not list(agy.parent.glob(".coordinator-status-line-*")) and not list(grok.parent.glob(".coordinator-status-line-*"))


def test_tui_status_lines_seed_absent_host_dirs_and_uninstall_removes_them(monkeypatch, tmp_path):
    _, agy, grok = _tui_home(monkeypatch, tmp_path)
    assert not agy.parent.exists() and not grok.parent.exists()
    rc, _ = _tui_main(tmp_path, "--install")
    assert rc == 0 and "statusLine" in json.loads(agy.read_text()) and "status_line" in tomllib.loads(grok.read_text())["ui"]
    rc, _ = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and not agy.exists() and not grok.exists()


def test_tui_status_default_hosts_include_grok_and_honour_grok_home(monkeypatch, tmp_path):
    home, agy, _ = _tui_home(monkeypatch, tmp_path)
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grokhome"))
    buf = io.StringIO()
    assert ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf) == 0
    assert (tmp_path / "grokhome" / "config.toml").is_file() and agy.is_file() and not (home / ".grok").exists()


def test_tui_status_lines_keep_user_customised_lines(monkeypatch, tmp_path):
    _, agy, grok = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    grok.parent.mkdir(parents=True)
    mine_agy = '{"statusLine": {"command": "my-line"}}\n'
    mine_grok = '[ui.status_line]\ntype = "command"\ncommand = "my-line"\n'
    agy.write_text(mine_agy)
    grok.write_text(mine_grok)
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and "agy status line retained-customized" in out and "grok status line retained-customized" in out
    agy_data = json.loads(agy.read_text())
    assert agy_data["statusLine"] == {"command": "my-line"} and agy_data["permissions"]["allow"] == [ui.agy_absolute_allow_rule()]
    assert grok.read_text() == mine_grok
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert "agy status line absent" in out and agy.read_text() == mine_agy and grok.read_text() == mine_grok


def test_tui_status_uninstall_after_user_edits_keeps_them_and_drops_only_ours(monkeypatch, tmp_path):
    _, agy, grok = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    agy.write_text('{"theme": "dark"}\n')
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text())
    data["added"] = 1
    agy.write_text(json.dumps(data))
    grok.write_bytes(grok.read_bytes() + b'added = 1\n')
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and "agy status line restored" in out and "grok status line restored" in out
    assert json.loads(agy.read_text()) == {"theme": "dark", "added": 1}
    assert tomllib.loads(grok.read_text()) == {"added": 1}
    # customised after install: retained
    agy.write_text('{"theme": "dark"}\n')
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text())
    data["statusLine"] = {"command": "later"}
    agy.write_text(json.dumps(data))
    _, out = _tui_main(tmp_path, "--uninstall", "agy")
    assert "agy status line retained-customized" in out and json.loads(agy.read_text())["statusLine"] == {"command": "later"}


def _fake_engine(tmp_path):
    pkg = tmp_path / "eng" / "coordinator_core" / "tui_status"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text("import sys\nprint('host=' + ' '.join(sys.argv[1:]))\n")
    return tmp_path / "eng"


def test_tui_status_command_runs_from_a_cwd_without_the_engine(tmp_path):
    root = _fake_engine(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    shutil.copy(ui._BIN_DIR / ui.TUI_STATUS_LAUNCHER, dest / ui.TUI_STATUS_LAUNCHER)
    cmd = ui._tui_status_command("agy", root, dest)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(cmd, shell=True, cwd=elsewhere, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "host=agy"
    assert ui._status_command_ours({"command": cmd}, "agy")
    assert not ui._status_command_ours({"command": cmd}, "grok")


def test_tui_status_command_is_launcher_with_no_space_or_quote_in_any_argument(monkeypatch):
    monkeypatch.setattr(ui, "_console_python", lambda: "\\py\\python.exe")
    cmd = ui._tui_status_command("agy", "\\eng\\root", "\\dest\\unimatrix")
    argv = shlex.split(cmd, posix=False)
    assert argv == ["/py/python.exe", "/dest/unimatrix/" + ui.TUI_STATUS_LAUNCHER, "/eng/root", "agy"]
    assert not any(c in a for a in argv for c in " \"'") and "-c" not in argv
    assert ui._status_command_ours({"command": cmd}, "agy")
    assert not ui._status_command_ours({"command": cmd}, "grok")


def test_tui_status_legacy_dash_c_form_is_ours_and_replaced(tmp_path):
    legacy = f'/py/python.exe -c "{ui._TUI_STATUS_BOOT}" /eng agy'
    assert ui._status_command_ours({"command": legacy}, "agy")
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"statusLine": {"command": legacy}}))
    assert ui.seed_host_status_line(cfg, "agy", engine_root="/eng", dest_dir=tmp_path / "u") == "updated"
    new = json.loads(cfg.read_text())["statusLine"]["command"]
    assert ui.TUI_STATUS_LAUNCHER in new and "-c" not in new.split()


def test_tui_status_install_upgrades_our_bare_module_form(monkeypatch, tmp_path):
    _, agy, grok = _tui_home(monkeypatch, tmp_path)
    root = _fake_engine(tmp_path)
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (str(root), "test"))
    agy.parent.mkdir(parents=True)
    grok.parent.mkdir(parents=True)
    agy.write_text(json.dumps({"theme": "dark", "statusLine": {"command": "/py/python3 -m coordinator_core.tui_status agy"}}))
    grok.write_text('x = 1\n[ui.status_line]\ntype = "command"\ncommand = "/py/python3 -m coordinator_core.tui_status grok"\n')
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy status line updated" in out and "grok status line updated" in out
    data = json.loads(agy.read_text())
    assert data["theme"] == "dark" and data["statusLine"]["command"] == ui._tui_status_command("agy", str(root), tmp_path / "dest")
    parsed = tomllib.loads(grok.read_text())
    assert parsed["x"] == 1 and parsed["ui"]["status_line"]["command"] == ui._tui_status_command("grok", str(root), tmp_path / "dest", grok.parent)
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy status line present" in out and "grok status line present" in out


def test_tui_status_grok_windows_command_is_one_ps1_path_and_script_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "_grok_ps1_form", lambda host: host == "grok")
    monkeypatch.setattr(ui, "_console_python", lambda: sys.executable)
    root = _fake_engine(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    shutil.copy(ui._BIN_DIR / ui.TUI_STATUS_LAUNCHER, dest / ui.TUI_STATUS_LAUNCHER)
    cfg = tmp_path / "grokhome" / "config.toml"
    cfg.parent.mkdir()
    legacy = f'/py/python.exe -c "{ui._TUI_STATUS_BOOT}" /eng grok'
    cfg.write_text('[ui.status_line]\ntype = "command"\ncommand = ' + json.dumps(legacy) + "\n")
    assert ui.seed_host_status_line(cfg, "grok", engine_root=str(root), dest_dir=dest) == "updated"
    cmd = tomllib.loads(cfg.read_text())["ui"]["status_line"]["command"]
    assert cmd == (cfg.parent / ui.GROK_STATUS_SCRIPT).as_posix() and not any(c in cmd for c in " \"'\\")
    assert (cfg.parent / ui.GROK_STATUS_SCRIPT).is_file()
    assert ui._status_command_ours({"command": cmd}, "grok") and not ui._status_command_ours({"command": cmd}, "agy")
    assert ui._status_command_ours({"command": legacy}, "grok")
    assert ui.seed_host_status_line(cfg, "grok", engine_root=str(root), dest_dir=dest) == "present"
    assert ui.seed_host_status_line(cfg, "grok", uninstall=True) == "restored"
    assert not (cfg.parent / ui.GROK_STATUS_SCRIPT).exists()
    cfg.write_text('[ui.status_line]\ntype = "command"\ncommand = "my-line"\n')
    assert ui.seed_host_status_line(cfg, "grok", engine_root=str(root), dest_dir=dest) == "retained-customized"


def test_tui_status_grok_legacy_command_in_toml_literal_string_is_replaced(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "_grok_ps1_form", lambda host: False)
    legacy = f'C:\\py\\python.exe -c "{ui._TUI_STATUS_BOOT}" D:\\eng grok'
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"x = 1\n[ui.status_line]\ntype = \"command\"\ncommand = '''{legacy}'''\n")
    assert ui.seed_host_status_line(cfg, "grok", engine_root="/eng", dest_dir=tmp_path / "u") == "updated"
    parsed = tomllib.loads(cfg.read_text())
    assert parsed["x"] == 1 and ui.TUI_STATUS_LAUNCHER in parsed["ui"]["status_line"]["command"]
    cfg.write_text('[ui.status_line]\ntype = "command"\ncommand = \'\'\'my-line\'\'\'\n')
    assert ui.seed_host_status_line(cfg, "grok", engine_root="/eng") == "retained-customized"


def test_tui_status_agy_empty_type_key_is_ours(tmp_path):
    """agy rewrites its settings and adds `"type": ""` to our statusLine; that value is still ours."""
    cfg = tmp_path / "settings.json"
    cfg.write_text(json.dumps({"theme": "dark", "statusLine": {"type": "", "command": "/py/python3 -m coordinator_core.tui_status agy"}}))
    assert ui.seed_host_status_line(cfg, "agy", engine_root="/eng") == "updated"
    want = ui._tui_status_command("agy", "/eng")
    assert json.loads(cfg.read_text())["statusLine"]["command"] == want
    assert ui.seed_host_status_line(cfg, "agy", engine_root="/eng") == "present"
    cfg.write_text(json.dumps({"theme": "dark", "statusLine": {"type": "", "command": want}}, indent=4))
    assert ui.seed_host_status_line(cfg, "agy", engine_root="/eng") == "present"
    fresh = tmp_path / "fresh" / "settings.json"
    assert ui.seed_host_status_line(fresh, "agy", engine_root="/eng") == "created"
    fresh.write_text(json.dumps({"statusLine": {"type": "", "command": want}, "added": 1}))
    assert ui.seed_host_status_line(fresh, "agy", uninstall=True) == "restored"
    assert json.loads(fresh.read_text()) == {"added": 1}


def test_tui_status_invalid_config_is_never_overwritten(tmp_path):
    cfg = tmp_path / "settings.json"
    cfg.write_text("{not json")
    with pytest.raises(ValueError):
        ui.seed_host_status_line(cfg, "agy")
    assert cfg.read_text() == "{not json"
    toml = tmp_path / "config.toml"
    toml.write_text("[ui\n")
    with pytest.raises(ValueError):
        ui.seed_host_status_line(toml, "grok")
    assert toml.read_text() == "[ui\n"


def _handoff_install(monkeypatch, tmp_path, *, live=True, change=False, pid_json=False, boom=None):
    """Install twice into one dest; the second install runs with a stub router seam. Returns (rc, out, calls)."""
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    dest = tmp_path / "d"
    assert _run(["--install"], "2.1.316", dest)[0] == 0
    if change:
        (tmp_path / "src" / ui.ROUTER_SCRIPT).write_text("# stub PipeServer v2\n", encoding="utf-8")
    if pid_json:
        (dest / "router").mkdir(exist_ok=True)
        (dest / "router" / "router.pid.json").write_text('{"pid": 1, "procStart": "x"}', encoding="utf-8")
    calls = []

    def restart(d, say):
        calls.append(d)
        if boom:
            raise boom
        return {"stopped": "handshake", "old_pid": 11, "new_pid": 22}

    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=dest, out=buf, hooks_path=tmp_path / "h.json",
                 router_live=lambda d: live, restart_router=restart)
    return rc, buf.getvalue(), calls


def test_changed_router_closure_with_live_router_restarts_once(monkeypatch, tmp_path):
    rc, out, calls = _handoff_install(monkeypatch, tmp_path, change=True, pid_json=True)
    assert rc == 0 and calls == [tmp_path / "d"]
    assert "router restarted: handshake old pid 11 new pid 22" in out


def test_unchanged_closure_leaves_a_handshake_router_alone(monkeypatch, tmp_path):
    rc, out, calls = _handoff_install(monkeypatch, tmp_path, pid_json=True)
    assert rc == 0 and calls == [] and "router restarted" not in out


def test_unchanged_closure_restarts_a_live_router_without_pid_record(monkeypatch, tmp_path):
    rc, out, calls = _handoff_install(monkeypatch, tmp_path)
    assert rc == 0 and len(calls) == 1 and "router restarted" in out


def test_changed_closure_without_a_live_router_never_restarts(monkeypatch, tmp_path):
    rc, _, calls = _handoff_install(monkeypatch, tmp_path, live=False, change=True)
    assert rc == 0 and calls == []


def test_restart_refusal_exits_8_and_keeps_the_copy(monkeypatch, tmp_path):
    rc, out, calls = _handoff_install(monkeypatch, tmp_path, change=True, boom=RuntimeError("lease pid 7 still held"))
    assert rc == 8 and len(calls) == 1
    assert "REFUSE: router restart failed: lease pid 7 still held" in out
    assert (tmp_path / "d" / ui.ROUTER_SCRIPT).read_text(encoding="utf-8") == "# stub PipeServer v2\n"


def test_router_closure_digest_tracks_closure_bytes(tmp_path):
    assert ui.router_closure_digest(tmp_path) is None
    (tmp_path / ui.ROUTER_SCRIPT).write_text("# a\n", encoding="utf-8")
    first = ui.router_closure_digest(tmp_path)
    assert first == ui.router_closure_digest(tmp_path)
    (tmp_path / ui.ROUTER_SCRIPT).write_text("# b\n", encoding="utf-8")
    assert ui.router_closure_digest(tmp_path) != first


def test_default_install_grants_agy_only_the_exact_submit_command(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    rc, _ = _tui_main(tmp_path, "--install")
    assert rc == 0
    allow = json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"]
    launcher = str(ui.settings_home() / "bin" / ("coordinator-invoke.exe" if sys.platform == "win32" else "coordinator-invoke"))
    assert allow == [f"command({launcher} engine_ask.submit)"]
    assert Path(launcher).is_absolute() and not any("*" in rule for rule in allow)


def test_agy_allow_rule_installs_once_keeps_operator_entries_and_uninstall_restores_bytes(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    mine = "command(Get-ChildItem ~/x)"
    before = json.dumps({"permissions": {"allow": [mine]}, "theme": "dark"}).encode("utf-8")
    agy.write_bytes(before)
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and "agy allow rule updated" in out
    allow = json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [mine, ui.agy_absolute_allow_rule()]
    once = agy.read_bytes()
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy allow rule present" in out and agy.read_bytes() == once
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and "agy allow rule restored" in out
    assert agy.read_bytes() == before
    assert not list(agy.parent.glob(".coordinator-*"))


def test_agy_allow_rule_uninstall_after_edit_removes_only_our_entry(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    mine = "command(keep me)"
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append(mine)
    agy.write_text(json.dumps(data), encoding="utf-8")
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and "agy allow rule restored" in out
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine]


def test_agy_allow_rule_reinstall_migrates_legacy_wildcard_and_uninstall_removes_both(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    mine = "command(keep me)"
    legacy = "command(coordinator-invoke *)"
    before = json.dumps({"permissions": {"allow": [mine]}}).encode("utf-8")
    agy.write_bytes(before)
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [mine, legacy]
    agy.write_text(json.dumps(data), encoding="utf-8")
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and "agy allow rule updated" in out
    allow = json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [mine, ui.agy_absolute_allow_rule()]
    once = agy.read_bytes()
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy allow rule present" in out and agy.read_bytes() == once
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and agy.read_bytes() == before

    agy.write_bytes(json.dumps({"permissions": {"allow": [mine, legacy]}}).encode("utf-8"))
    _tui_main(tmp_path, "--install")
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine, legacy, ui.agy_absolute_allow_rule()]
    _tui_main(tmp_path, "--uninstall")

    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [mine, legacy, ui.AGY_ALLOW_RULE]
    agy.write_text(json.dumps(data), encoding="utf-8")
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append(legacy)
    agy.write_text(json.dumps(data), encoding="utf-8")
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine]


def test_agy_allow_rule_default_grants_absolute_line_and_reports_it(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    absolute = ui.agy_absolute_allow_rule()
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and f"agy allow rule granted: {absolute}" in out
    allow = json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [absolute] and ui.AGY_ALLOW_RULE not in allow
    # agy `permissions.allow` entries have the form `command(<cmd>)`.
    inner = allow[0][len("command("):-1]
    assert Path(inner.split(" ", 1)[0]).is_absolute()


def test_agy_allow_rule_is_the_advisory_command_and_prior_forms_are_owned(monkeypatch, tmp_path):
    from coordinator_core import warp_fire

    assert ui.AGY_ALLOW_RULE == f"command({warp_fire.INVOKE_SUBMIT_COMMAND})"
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    mine = "command(keep me)"
    _tui_main(tmp_path, "--install")
    agy.write_text(json.dumps({"permissions": {"allow": [mine, ui.AGY_PRIOR_ALLOW_RULE]}}), encoding="utf-8")
    _tui_main(tmp_path, "--install")
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine, ui.agy_absolute_allow_rule()]
    assert ui.AGY_PRIOR_ALLOW_RULE not in json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"]


def test_agy_allow_rule_migrates_bare_and_legacy_lines_to_absolute_and_uninstalls(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    mine = "command(keep me)"
    legacy = "command(coordinator-invoke *)"
    before = json.dumps({"permissions": {"allow": [mine]}}).encode("utf-8")
    agy.write_bytes(before)
    absolute = ui.agy_absolute_allow_rule()
    assert absolute != ui.AGY_ALLOW_RULE and str(ui.settings_home() / "bin" / "coordinator-invoke") in absolute
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and f"agy allow rule granted: {absolute}" in out
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine, absolute]
    data = json.loads(agy.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [mine, legacy, ui.AGY_ALLOW_RULE]
    agy.write_text(json.dumps(data), encoding="utf-8")
    _tui_main(tmp_path, "--install")
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine, absolute]
    _tui_main(tmp_path, "--install")
    rc, _ = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and agy.read_bytes() == before


# `"toolPermission": "always-proceed"` in agy's settings.json approves every tool call regardless of --mode.
_BYPASS = {"toolPermission": "always-proceed"}


def test_install_removes_always_proceed_and_uninstall_restores_the_file_byte_for_byte(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    before = json.dumps({**_BYPASS, "theme": "dark"}).encode("utf-8")
    agy.write_bytes(before)
    rc, out = _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    assert rc == 0 and "toolPermission" not in data and "always-proceed" not in agy.read_text(encoding="utf-8")
    assert data["permissions"]["allow"] == [ui.agy_absolute_allow_rule()] and data["theme"] == "dark"
    once = agy.read_bytes()
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy allow rule present" in out and agy.read_bytes() == once
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and agy.read_bytes() == before


def test_install_removes_always_proceed_agy_rewrote_after_install_and_keeps_the_original_restore(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    before = json.dumps({"permissions": {"allow": ["command(keep me)"]}, **_BYPASS}).encode("utf-8")
    agy.write_bytes(before)
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    data.update(_BYPASS)
    agy.write_text(json.dumps(data), encoding="utf-8")
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy allow rule updated" in out and "toolPermission" not in json.loads(agy.read_text(encoding="utf-8"))
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and agy.read_bytes() == before


def test_uninstall_after_edit_restores_the_recorded_always_proceed_and_keeps_operator_entries(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    agy.write_bytes(json.dumps(_BYPASS).encode("utf-8"))
    _tui_main(tmp_path, "--install")
    data = json.loads(agy.read_text(encoding="utf-8"))
    data["permissions"]["allow"].append("command(keep me)")
    agy.write_text(json.dumps(data), encoding="utf-8")
    rc, out = _tui_main(tmp_path, "--uninstall")
    after = json.loads(agy.read_text(encoding="utf-8"))
    assert rc == 0 and after == {"permissions": {"allow": ["command(keep me)"]}, **_BYPASS}


def test_install_leaves_a_non_bypass_tool_permission_untouched(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    agy.write_bytes(json.dumps({"toolPermission": "request-review"}).encode("utf-8"))
    _tui_main(tmp_path, "--install")
    assert json.loads(agy.read_text(encoding="utf-8"))["toolPermission"] == "request-review"
    rc, _ = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and json.loads(agy.read_text(encoding="utf-8")) == {"toolPermission": "request-review"}


def test_install_agy_skills_drops_scratch_roots_and_adds_no_coordinator_source(monkeypatch, tmp_path):
    monkeypatch.setattr(ui._seed(), "is_scratch_root", lambda p: str(p).startswith("/private/tmp"))
    seed = ui._seed()
    sj = tmp_path / "skills.json"
    ok = tmp_path / "ok" / "skills"
    ok.mkdir(parents=True)
    # Source: observed ~/.gemini/config/skills.json listing another session's /private/tmp scratchpad (R1 item 5).
    sj.write_text(json.dumps({"entries": [{"path": "/private/tmp/claude/scratchpad/skills"}, {"path": str(ok)}]}))
    assert seed.seed_agy_skills(sj, "/p/plugin/skills") == "pruned"
    assert [e["path"] for e in json.loads(sj.read_text())["entries"]] == [str(ok)]
    assert seed.seed_agy_skills(sj, "/p/plugin/skills") == "present"


def test_install_heals_a_stale_profile_in_one_run_and_a_second_run_is_idempotent(monkeypatch, tmp_path):
    plugin = _engine_tree_and_plugin(monkeypatch, tmp_path)
    users = tmp_path / "Users"
    home = users / "me"
    stale = users / "me_old"
    for d in (home, stale / "skills"):
        d.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    gem = home / ".gemini"
    (gem / "config").mkdir(parents=True)
    cache = tmp_path / "cache" / "coordinator" / "4.6.21" / "skills"
    klab = tmp_path / "klabauter" / "coordinator" / "skills"
    for d in (cache, klab):
        d.mkdir(parents=True)
    foreign = tmp_path / "foreign-skills"
    foreign.mkdir()
    (gem / "config" / "plugins.json").write_text(json.dumps({"entries": [
        {"path": str(stale), "include_only": ["agy-plugin"]}, {"path": str(tmp_path / "gone")}]}), encoding="utf-8")
    (gem / "config" / "skills.json").write_text(json.dumps({"entries": [
        {"path": str(cache)}, {"path": str(klab)}, {"path": str(stale / "skills")}, {"path": str(foreign)}]}), encoding="utf-8")
    old_python = stale / "python.exe"
    (gem / "GEMINI.md").write_text(
        f"mine\n{ui.DOCTRINE_BEGIN}\n{old_python} {stale}/unimatrix-send.py\n{ui.DOCTRINE_END}\n", encoding="utf-8")
    pins = [str(old_python)]
    monkeypatch.setattr(ui, "_pin_raw", lambda: pins[-1])
    monkeypatch.setattr(ui, "_write_pin", lambda interp: pins.append(interp) or True)
    argv = ("--install", "--hosts", "agy", "--plugin-root", str(plugin))

    def run():
        buf = io.StringIO()
        rc = ui.main(list(argv), version_text="2.1.316", dest_dir=tmp_path / "dest", out=buf)
        return rc, buf.getvalue()

    rc, out = run()
    assert rc == 0, out
    assert pins[-1] == sys.executable and len(pins) == 2
    tree = tmp_path / "dest" / "agy-plugin"
    assert json.loads((gem / "config" / "plugins.json").read_text(encoding="utf-8"))["entries"] == [
        {"path": str(tree.parent), "include_only": ["agy-plugin"]}]
    assert json.loads((gem / "config" / "skills.json").read_text(encoding="utf-8"))["entries"] == [{"path": str(foreign)}]
    text = (gem / "GEMINI.md").read_text(encoding="utf-8")
    assert "mine" in text and _fwd(stale) not in text and _fwd(tmp_path / "dest") in text
    snap = {p: p.read_bytes() for p in (gem / "config" / "plugins.json", gem / "config" / "skills.json", gem / "GEMINI.md")}
    pins.append(__file__)
    rc, out = run()
    assert rc == 0 and len(pins) == 3
    assert {p: p.read_bytes() for p in snap} == snap


def test_agy_allow_rule_refuses_a_non_list_allow_without_writing(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    agy.write_text('{"permissions": {"allow": "x"}}', encoding="utf-8")
    rc, out = _tui_main(tmp_path, "--install", hosts="agy")
    assert "WARN: agy allow rule not installed" in out
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"] == {"allow": "x"}


@pytest.mark.parametrize("alias,host", [("mac-mini", "mac-mini.local"), ("me@mac-mini", "mac-mini.local"),
                                        ("box.example.org", "box.example.org")])
def test_ssh_config_block_names_the_peer_by_hostname_never_an_ip(alias, host):
    block = ui.render_ssh_config_block(alias, Path("/k/key"))
    assert f"    HostName {host}\n" in block
    assert "    AddressFamily inet\n" in block
    assert "<address" not in block
    assert f"Host {alias}\n" in block


def test_control_line_names_the_control_script_and_comment():
    line = ui.control_authorized_keys_line("KB", ["10.0.0.0/8", "fd00::/8"])
    assert line == 'from="10.0.0.0/8,fd00::/8" ssh-ed25519 KB unimatrix-control'
    assert "command=" not in line and "restrict" not in line
    with pytest.raises(TypeError):
        ui.authorized_keys_line("/d", "/p", control=True)


def test_default_install_prints_a_control_key_line_after_the_relay_and_file_lines(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    rc, out = _run(["--install"], "2.1.316", dest, tmp_path / "gemini" / "hooks.json")
    assert rc == 0
    lines = [l for l in out.splitlines() if "restrict,command=" in l]
    assert len(lines) == 2 and "unimatrix-relay.py" in lines[0] and "unimatrix-file-relay.py" in lines[1]
    control = next(l for l in out.splitlines() if l.endswith(" unimatrix-control"))
    assert 'from="<from-cidrs>" ssh-ed25519 <key-body> unimatrix-control' in control
    assert "command=" not in control and "restrict" not in control
    assert {"unimatrix-control.py", "headless-em-launch.py"} <= {p.name for p in dest.iterdir()}


def test_install_vendors_session_detach_beside_the_launch_chain(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    (tmp_path / "src" / "lib").mkdir()
    (tmp_path / "src" / "lib" / "session_detach.py").write_text("# stub\n", encoding="utf-8")
    dest = tmp_path / "home" / "unimatrix"
    rc, _ = _run(["--install"], "2.1.316", dest, tmp_path / "gemini" / "hooks.json")
    assert rc == 0 and (dest / "lib" / "session_detach.py").is_file()


def test_membership_without_this_box_or_with_empty_cidrs_is_no_pin(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    membership = ui._sibling("unimatrix-membership.py")
    monkeypatch.setattr(ui._sibling("unimatrix-peer-contract.py"), "local_box", lambda: "me")
    for members in ([{"box": "other", "from_cidrs": ["1.1.1.0/24"]}], [{"box": "me", "from_cidrs": []}]):
        monkeypatch.setattr(membership, "verified_members", lambda home, run=None, m=members: m)
        with pytest.raises(ValueError, match="--from-cidr"):
            ui.control_from_cidrs(None)


_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabcdefgh unimatrix"


def _install_with_keys(monkeypatch, tmp_path, text):
    _stub_src(monkeypatch, tmp_path)
    keys = tmp_path / "ssh" / "authorized_keys"
    keys.parent.mkdir()
    keys.write_bytes(text.encode("utf-8"))
    keys.chmod(0o640)
    buf = io.StringIO()
    rc = ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "home" / "unimatrix", out=buf,
                 hooks_path=tmp_path / "gemini" / "hooks.json", authorized_keys=keys)
    return rc, buf.getvalue(), keys


def test_install_rewrites_a_stale_forced_interpreter_and_only_that(monkeypatch, tmp_path):
    new = ui.forced_command_interpreter()
    stale = tmp_path / "stale" / "python3"
    other = f'restrict,command="{stale} /d/unimatrix-file-relay.py --peer b" {_KEY}'
    commented = f'# restrict,command="{stale} /d/unimatrix-relay.py" {_KEY}'
    bare = f'restrict,command="python3 /d/unimatrix-relay.py" {_KEY}'
    unrelated = "ssh-rsa AAAAB3 me@box"
    text = "\n".join([f'restrict,command="{stale} /d/unimatrix-relay.py --x" {_KEY}', other, commented, bare, unrelated]) + "\r\n"
    rc, out, keys = _install_with_keys(monkeypatch, tmp_path, text)
    assert rc == 0 and f"rewritten to {new}" in out
    assert keys.read_bytes().decode() == text.replace(str(stale), new, 1)
    if os.name == "posix":
        assert keys.stat().st_mode & 0o777 == 0o640


def test_install_leaves_an_already_correct_forced_interpreter_untouched(monkeypatch, tmp_path):
    text = f'restrict,command="{ui.forced_command_interpreter()} /d/unimatrix-relay.py" {_KEY}\n'
    rc, out, keys = _install_with_keys(monkeypatch, tmp_path, text)
    assert rc == 0 and "rewritten" not in out and keys.read_text(encoding="utf-8") == text


def test_rewrite_handles_a_windows_shaped_quoted_interpreter_and_a_spaced_target(tmp_path):
    keys = tmp_path / "authorized_keys"
    keys.write_text(f'restrict,command="\\"C:\\Old Py\\python.exe\\" \\"C:\\u\\unimatrix-relay.py\\"" {_KEY}\n', encoding="utf-8")  # abs-path-ok: synthetic Windows line, never opened
    assert ui.rewrite_forced_interpreters(keys, "D:/new py/python.exe") == 1
    assert keys.read_text(encoding="utf-8") == (  # abs-path-ok: synthetic Windows line, never opened
        f'restrict,command="\\"D:/new py/python.exe\\" \\"C:\\u\\unimatrix-relay.py\\"" {_KEY}\n')
    assert ui.rewrite_forced_interpreters(keys, "D:/new py/python.exe") == 0
    assert ui.rewrite_forced_interpreters(tmp_path / "absent", "/x/python3") == 0  # abs-path-ok: synthetic, never opened


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_rewrite_keeps_a_symlinked_authorized_keys_a_symlink(tmp_path):
    real = tmp_path / "real_keys"
    real.write_text(f'restrict,command="{tmp_path}/old/python3 /d/unimatrix-relay.py" {_KEY}\n', encoding="utf-8")
    link = tmp_path / "authorized_keys"
    link.symlink_to(real)
    assert ui.rewrite_forced_interpreters(link, "/x/python3") == 1  # abs-path-ok: synthetic interpreter, never opened
    assert link.is_symlink() and "/x/python3" in real.read_text(encoding="utf-8")  # abs-path-ok: synthetic, never opened


def test_every_lib_module_an_installed_script_loads_by_path_is_vendored():
    # A lib/ module missing from VENDORED_LIB installs fine and dies at first use on the far box
    # (r25.14: unimatrix-mirror-all.py's pair contract, the Mini inbox supervisor never started).
    import re
    pat = re.compile(r'"lib"\s*/\s*"([A-Za-z0-9_]+\.py)"')
    installed = set(ui.VENDORED_SCRIPTS) | set(ui.script_closure(ui._BIN_DIR)) | set(ui.OPTIONAL_SCRIPTS)
    loaded = {}
    for name in sorted(installed):
        path = ui._BIN_DIR / name
        if path.is_file():
            for mod in pat.findall(path.read_text(encoding="utf-8")):
                loaded.setdefault(mod, name)
    assert "unimatrix_pair_contract.py" in loaded, "the scan no longer sees the mirror-all load; fix the pattern"
    missing = {m: s for m, s in loaded.items() if m not in ui.VENDORED_LIB and m not in ui.UNSHIPPED_LIB}
    assert not missing, f"lib modules loaded by installed scripts but not vendored: {missing}"


def test_install_scripts_copies_the_pair_contract(tmp_path):
    ui.install_scripts(ui._BIN_DIR, tmp_path)
    assert (tmp_path / "lib" / "unimatrix_pair_contract.py").is_file()


def _launchctl_recorder(loaded=True):
    calls = []

    def run(*args):
        calls.append(args)
        rc = 0 if (args[0] != "print" or loaded) else 113
        return subprocess.CompletedProcess(["launchctl", *args], rc, "", "")
    return calls, run


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd only")
def test_updated_plist_reloads_the_loaded_agent(tmp_path):
    # launchd keeps the old ProgramArguments until logout: the Mini's agent ran python3.15 for hours
    # after the plist named pyenv 3.13, and only the stale binary lacked the Local Network grant.
    (tmp_path / f"{ui.LOGIN_AGENT_LABEL}.plist").write_bytes(b"stale")
    calls, run = _launchctl_recorder(loaded=True)
    state, where = ui.register_login_agent(tmp_path, tmp_path, launchctl=run, uid=501)
    assert state == "updated and reloaded"
    assert [c[0] for c in calls] == ["print", "bootout", "bootstrap"]
    assert calls[2] == ("bootstrap", "gui/501", where)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd only")
def test_unchanged_or_unloaded_agent_is_not_reloaded(tmp_path):
    calls, run = _launchctl_recorder(loaded=True)
    ui.register_login_agent(tmp_path, tmp_path, launchctl=run, uid=501)
    assert ui.register_login_agent(tmp_path, tmp_path, launchctl=run, uid=501)[0] == "present"
    assert calls == []
    (tmp_path / f"{ui.LOGIN_AGENT_LABEL}.plist").write_bytes(b"stale")
    calls2, run2 = _launchctl_recorder(loaded=False)
    assert ui.register_login_agent(tmp_path, tmp_path, launchctl=run2, uid=501)[0] == "updated"
    assert [c[0] for c in calls2] == ["print"]


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd only")
def test_custom_agent_dir_without_injected_launchctl_never_touches_launchd(tmp_path, monkeypatch):
    (tmp_path / f"{ui.LOGIN_AGENT_LABEL}.plist").write_bytes(b"stale")
    monkeypatch.setattr(ui, "run_launchctl", lambda *a: pytest.fail(f"real launchctl called: {a}"))
    assert ui.register_login_agent(tmp_path, tmp_path)[0] == "updated"


def test_grok_hook_skipped_while_only_a_grok_home_exists_and_seeded_once_grok_is_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "host_present", _REAL_HOST_PRESENT)
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    (home / ".grok").mkdir(exist_ok=True)
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "grok hook skipped" in buf.getvalue() and "grok hook created" not in buf.getvalue()
    assert not (home / ".grok" / "hooks").exists()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "grok").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(bindir))
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "grok hook created" in buf.getvalue() and (home / ".grok" / "hooks" / "coordinator.json").is_file()
    assert not (home / ".grok" / "hooks.json").exists()


def test_grok_hooks_path_is_the_grok_loaded_hooks_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "gh"))
    assert ui.grok_hooks_path() == tmp_path / "gh" / "hooks" / "coordinator.json"
    monkeypatch.delenv("GROK_HOME")
    _home(monkeypatch, tmp_path)
    assert ui.grok_hooks_path() == Path.home() / ".grok" / "hooks" / "coordinator.json"


def test_grok_hook_seeded_when_grok_binary_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "host_present", _REAL_HOST_PRESENT)
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "grok").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(bindir))
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "grok hook created" in buf.getvalue()


def _bare_env(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    for var in ("GROK_HOME", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.setattr(ui, "host_present", _REAL_HOST_PRESENT)
    return home


def test_install_with_every_host_absent_writes_nothing_for_any_host(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _bare_env(monkeypatch, tmp_path)
    buf = io.StringIO()
    assert ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf) in (0, 7), buf.getvalue()
    for host in ("codex", "agy", "grok"):
        assert f"{host} hook skipped" in buf.getvalue()
    assert not any((home / name).exists() for name in (".codex", ".gemini", ".grok", ".agents")), sorted(p.name for p in home.iterdir())
    written = "\n".join(ln for ln in buf.getvalue().splitlines() if "skipped" not in ln)
    for surface in ("status line", "peer doctrine", "coordinator doctrine", "send rule", "status footer"):
        assert surface not in written


def test_default_install_with_codex_and_grok_homes_but_not_on_path_writes_no_codex_or_grok_config(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _bare_env(monkeypatch, tmp_path)
    (home / ".codex").mkdir()
    (home / ".grok").mkdir()
    buf = io.StringIO()
    ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf)
    assert "codex hook skipped" in buf.getvalue() and "grok hook skipped" in buf.getvalue()
    assert "no codex home" not in buf.getvalue() and "no grok home" not in buf.getvalue()
    assert sorted(p.name for p in (home / ".codex").iterdir()) == []
    assert sorted(p.name for p in (home / ".grok").iterdir()) == []


def test_install_seeds_only_the_present_host(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _bare_env(monkeypatch, tmp_path)
    (home / ".grok").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "codex").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(bindir))
    buf = io.StringIO()
    ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf)
    assert (home / ".codex" / "hooks.json").is_file() and (home / ".codex" / "AGENTS.md").is_file()
    assert not (home / ".grok" / "hooks").exists() and not (home / ".gemini").exists()
    assert "codex hook skipped" not in buf.getvalue() and "agy hook skipped" in buf.getvalue()
    assert "grok hook skipped" in buf.getvalue()


def test_host_present_reads_path_and_home(monkeypatch, tmp_path):
    _bare_env(monkeypatch, tmp_path)
    assert not any(ui.host_present(h) for h in ("codex", "agy", "grok"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "agy.exe").write_text("x")
    monkeypatch.setenv("PATH", str(bindir))
    assert ui.host_present("agy") and not ui.host_present("codex")
    (Path.home() / ".codex").mkdir()
    (Path.home() / ".grok").mkdir()
    assert not ui.host_present("codex") and not ui.host_present("grok")
    (bindir / "codex.exe").write_text("x")
    (bindir / "grok.cmd").write_text("x")
    assert ui.host_present("codex") and ui.host_present("grok")


def _env_with_pwsh_dirs(tmp_path, *dirs):
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / "pwsh.exe").write_text("x")
    return {"PATH": os.pathsep.join(str(tmp_path / d) for d in dirs)}


def test_pwsh_alias_remedy_fires_only_for_a_windowsapps_first_pwsh(tmp_path):
    env = _env_with_pwsh_dirs(tmp_path, "Microsoft/WindowsApps", "Other")
    text = _REAL_PWSH_ALIAS_REMEDY(env, "win32")
    assert "Store/MSIX" in text and "--installer-type wix" in text
    assert _REAL_PWSH_ALIAS_REMEDY(_env_with_pwsh_dirs(tmp_path, "Other", "Microsoft/WindowsApps"), "win32") is None
    assert _REAL_PWSH_ALIAS_REMEDY(env, "linux") is None
    assert _REAL_PWSH_ALIAS_REMEDY({"PATH": str(tmp_path / "none")}, "win32") is None


def test_install_prints_the_pwsh_alias_remedy(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "pwsh_alias_remedy", lambda *a, **k: "pwsh resolves first to the Store/MSIX alias X; remedy: R")
    buf = io.StringIO()
    ui.main(["--install"], version_text="2.1.316", dest_dir=tmp_path / "d", out=buf, hooks_path=tmp_path / "h.json")
    assert "WARN: pwsh resolves first to the Store/MSIX alias X; remedy: R" in buf.getvalue()


def test_powershell_host_prefers_msi_then_nonstore_pwsh_then_5_1_and_never_msix(tmp_path):
    pf, root = tmp_path / "pf", tmp_path / "win"
    ps51 = root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    ps51.parent.mkdir(parents=True)
    ps51.write_text("x")
    env = {"ProgramFiles": str(pf), "SystemRoot": str(root), **_env_with_pwsh_dirs(tmp_path, "Microsoft/WindowsApps")}
    assert _REAL_POWERSHELL_HOST(env, "win32") == str(ps51)
    (pf / "PowerShell" / "7").mkdir(parents=True)
    (pf / "PowerShell" / "7" / "pwsh.exe").write_text("x")
    assert _REAL_POWERSHELL_HOST(env, "win32") == str(pf / "PowerShell" / "7" / "pwsh.exe")
    assert _REAL_POWERSHELL_HOST(env, "linux") is None


_SSH_OK = {"installed": True, "running": True, "automatic": True, "firewall22": True, "keys_exists": True,
           "keys_protected": True, "keys_sids": ["S-1-5-18", "S-1-5-32-544"]}


def test_openssh_problems_names_each_missing_requirement():
    assert ui.openssh_problems(_SSH_OK) == []
    names = [n for n, _ in ui.openssh_problems({})]
    assert names == ["openssh-server-installed", "openssh-sshd-running", "openssh-sshd-automatic",
                     "openssh-firewall-22", "openssh-admin-keys-acl"]
    wide = dict(_SSH_OK, keys_sids=["S-1-5-18", "S-1-5-32-544", "S-1-5-32-545"])
    assert [n for n, _ in ui.openssh_problems(wide)] == ["openssh-admin-keys-acl"]
    assert [n for n, _ in ui.openssh_problems(dict(_SSH_OK, keys_protected=False))] == ["openssh-admin-keys-acl"]


def test_agy_matcher_covers_every_adapter_edit_tool():
    """An agy edit tool the engine guards but the matcher omits never reaches the guard."""
    # Trap: DoE byte-resyncs coordinator/bin without coordinator_core, so the engine import may be absent.
    agy_adapter = pytest.importorskip("coordinator_core.host_hooks.agy_adapter")
    names = ui.AGY_TOOL_MATCHER.split("|")
    assert "run_command" in names
    assert set(agy_adapter._EDIT_TOOLS) <= set(names)


def test_install_widens_an_older_run_command_only_entry(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "home" / "unimatrix"
    hp = tmp_path / "hooks.json"
    narrow = ui.agy_hook_entry(dest)
    narrow["PreToolUse"][0]["matcher"] = "run_command"
    hp.write_text(json.dumps({ui.AGY_HOOK_KEY: narrow}), encoding="utf-8")
    rc, out = _run(["--install"], "2.1.316", dest, hp)
    assert rc == 0 and "updated" in out
    entry = json.loads(hp.read_text(encoding="utf-8"))[ui.AGY_HOOK_KEY]["PreToolUse"][0]
    assert entry["matcher"] == ui.AGY_TOOL_MATCHER


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits only")


@posix_only
def test_install_scripts_dest_is_0755_under_umask_077(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "newhome" / "unimatrix"
    old = os.umask(0o077)
    try:
        ui.install_scripts(tmp_path / "src", dest)
    finally:
        os.umask(old)
    assert oct(dest.stat().st_mode & 0o777) == "0o755"
    assert oct(dest.parent.stat().st_mode & 0o777) == "0o755"


@posix_only
def test_install_scripts_leaves_an_existing_dest_mode_alone(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    dest = tmp_path / "unimatrix"
    dest.mkdir()
    dest.chmod(0o700)
    ui.install_scripts(tmp_path / "src", dest)
    assert oct(dest.stat().st_mode & 0o777) == "0o700"


@posix_only
def test_preflight_prints_chmod_remedy_for_untraversable_path_dir(monkeypatch, tmp_path):
    shut = tmp_path / "shutbin"
    shut.mkdir()
    shut.chmod(0o700)
    ok = tmp_path / "openbin"
    ok.mkdir()
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, m, **k: False if str(p) == str(shut) else real_access(p, m, **k))
    monkeypatch.setenv("PATH", os.pathsep.join([str(shut), str(ok), str(tmp_path / "absent")]))
    rc, out = _run([], "2.1.316")
    assert rc == 0
    assert f"sudo chmod 755 {shut}" in out
    assert str(ok) not in out and str(tmp_path / "absent") not in out


@posix_only
def test_traversable_path_dirs_report_nothing(monkeypatch, tmp_path):
    assert ui.untraversable_path_dirs(str(tmp_path)) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows branch")
def test_preflight_is_a_noop_on_windows(tmp_path):
    assert ui.untraversable_path_dirs(str(tmp_path)) == []


def _grok_world(tmp_path, monkeypatch, system_writable):
    home = tmp_path / "fakehome"
    home.mkdir()
    brew = tmp_path / "brew"
    brew.mkdir()
    (brew / "grok").write_text("#!/bin/sh\n", encoding="utf-8")
    (brew / "grok").chmod(0o755)
    system = tmp_path / "usrlocalbin"
    system.mkdir()
    real_access = os.access
    monkeypatch.setattr(os, "access", lambda p, m, **k: system_writable if str(p) == str(system) else real_access(p, m, **k))
    monkeypatch.setattr(ui, "GROK_BIN_HINTS", ())
    return home, brew, system


@posix_only
def test_grok_linked_into_local_bin_when_system_bin_unwritable(monkeypatch, tmp_path):
    home, brew, system = _grok_world(tmp_path, monkeypatch, system_writable=False)
    assert _REAL_LINK_GROK(home, str(brew), system) == "linked"
    link = home / ".local" / "bin" / "grok"
    assert link.is_symlink() and link.resolve() == (brew / "grok").resolve()
    assert oct((home / ".local" / "bin").stat().st_mode & 0o777) == "0o755"
    assert _REAL_LINK_GROK(home, str(brew), system) == "present"


@posix_only
def test_grok_not_linked_when_system_bin_writable(monkeypatch, tmp_path):
    home, brew, system = _grok_world(tmp_path, monkeypatch, system_writable=True)
    assert _REAL_LINK_GROK(home, str(brew), system) == "skipped"
    assert not (home / ".local").exists()


@posix_only
def test_grok_link_absent_without_installed_grok_and_never_clobbers(monkeypatch, tmp_path):
    home, brew, system = _grok_world(tmp_path, monkeypatch, system_writable=False)
    assert _REAL_LINK_GROK(home, str(tmp_path / "empty"), system) == "absent"
    own = home / ".local" / "bin"
    own.mkdir(parents=True)
    (own / "grok").write_text("mine", encoding="utf-8")
    assert _REAL_LINK_GROK(home, str(brew), system) == "present"
    assert (own / "grok").read_text(encoding="utf-8") == "mine"


@posix_only
def test_grok_link_never_spawns_sudo(monkeypatch, tmp_path):
    home, brew, system = _grok_world(tmp_path, monkeypatch, system_writable=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("subprocess spawned"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("subprocess spawned"))
    assert _REAL_LINK_GROK(home, str(brew), system) == "linked"


@posix_only
def test_install_default_run_links_grok(monkeypatch, tmp_path):
    home, brew, system = _grok_world(tmp_path, monkeypatch, system_writable=False)
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(ui, "link_grok_local_bin", lambda: _REAL_LINK_GROK(home, str(brew), system))
    rc, out = _run(["--install"], "2.1.316", tmp_path / "dest")
    assert rc == 0
    assert "grok ~/.local/bin link linked" in out
    assert (home / ".local" / "bin" / "grok").is_symlink()


_OLD = (3, 9, 6)  # a fake pre-3.11 sys.version_info for the gate


def test_gate_is_a_noop_on_a_modern_interpreter():
    seen = []
    ui.ensure_modern_python(version_info=(3, 11, 0), env={}, reexec=lambda *a: seen.append(a))
    ui.ensure_modern_python(env={}, reexec=lambda *a: seen.append(a))
    assert seen == []


def test_gate_reexecs_homebrew_with_same_argv_on_darwin(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n", encoding="utf-8")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(tmp_path / "absent"), str(brew)))
    seen, env = [], {}
    ui.ensure_modern_python(version_info=_OLD, platform="darwin", env=env, pin=lambda: None,
                            probe=lambda exe: True, reexec=lambda exe, argv: seen.append((exe, argv)),
                            argv=["x.py", "--install"])
    assert seen == [(str(brew), ["x.py", "--install"])]
    assert env[ui._REEXEC_ENV] == "1"


def test_gate_prefers_a_qualifying_pin_over_homebrew(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n", encoding="utf-8")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(brew),))
    seen = []
    ui.ensure_modern_python(version_info=_OLD, platform="darwin", env={}, pin=lambda: "/pinned/python3",
                            probe=lambda exe: True, reexec=lambda exe, argv: seen.append(exe), argv=["x.py"])
    assert seen == ["/pinned/python3"]


def test_gate_skips_a_too_old_pin_for_a_qualifying_homebrew(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n", encoding="utf-8")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(brew),))
    seen = []
    ui.ensure_modern_python(version_info=_OLD, platform="darwin", env={}, pin=lambda: "/pinned/old",
                            probe=lambda exe: exe != "/pinned/old", reexec=lambda exe, argv: seen.append(exe),
                            argv=["x.py"])
    assert seen == [str(brew)]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_gate_probes_homebrew_only_on_darwin(monkeypatch, tmp_path, platform):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n", encoding="utf-8")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(brew),))
    seen = []
    with pytest.raises(SystemExit) as exc:
        ui.ensure_modern_python(version_info=_OLD, platform=platform, env={}, pin=lambda: None,
                                probe=lambda exe: True, reexec=lambda exe, argv: seen.append(exe), argv=["x.py"])
    assert seen == []
    assert "3.11" in str(exc.value.code)


def test_gate_exits_naming_the_version_when_no_candidate_qualifies(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(tmp_path / "absent"),))
    seen = []
    with pytest.raises(SystemExit) as exc:
        ui.ensure_modern_python(version_info=_OLD, platform="darwin", env={}, pin=lambda: "/pinned/old",
                                probe=lambda exe: False, reexec=lambda exe, argv: seen.append(exe), argv=["x.py"])
    msg = str(exc.value.code)
    assert seen == []
    assert "Python 3.11" in msg and "3.9.6" in msg and "tomllib" not in msg


def test_gate_refuses_a_second_reexec(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n", encoding="utf-8")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(brew),))
    seen = []
    with pytest.raises(SystemExit) as exc:
        ui.ensure_modern_python(version_info=_OLD, platform="darwin", env={ui._REEXEC_ENV: "1"}, pin=lambda: None,
                                probe=lambda exe: True, reexec=lambda exe, argv: seen.append(exe), argv=["x.py"])
    assert seen == []
    assert "3.11" in str(exc.value.code)


def test_gate_probe_accepts_this_interpreter_and_rejects_a_missing_one(tmp_path):
    assert ui._runs_modern(sys.executable) is True
    assert ui._runs_modern(str(tmp_path / "absent")) is False


def test_gate_runs_before_the_tomllib_import():
    src = _SRC.read_text(encoding="utf-8")
    assert src.index("\nensure_modern_python()\n") < src.index("\nimport tomllib\n")


def test_grok_present_survives_an_unreadable_path_entry(tmp_path, monkeypatch):
    """An EACCES PATH directory (the Mini's /usr/local/bin/grok) must read absent, never abort the install."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    monkeypatch.setenv("PATH", str(locked))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "no-grok-home"))
    try:
        assert ui.grok_present() is False
    finally:
        locked.chmod(0o755)


_REAL_NAMES = json.loads((Path(__file__).parent / "fixtures" / "host-real-tool-names.json").read_text(encoding="utf-8"))


def _written_matcher(host, tmp_path):
    """The matcher the installer writes for `host`, read from the entry it builds (grok: the file seed_grok_hook writes)."""
    if host == "grok":
        ghp = tmp_path / "hooks" / "coordinator.json"
        ui.seed_grok_hook(ghp, tmp_path)
        groups = json.loads(ghp.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        return next(g["matcher"] for g in groups if any(ui.GUARD_SCRIPT in h["command"] for h in g["hooks"]))
    if host == "agy":
        return ui.agy_hook_entry(tmp_path)["PreToolUse"][0]["matcher"]
    return ui.codex_hook_events(tmp_path)["PreToolUse"]["matcher"]


@pytest.mark.parametrize("host", ["grok", "agy", "codex"])
def test_real_tool_fixture_entries_each_name_a_source_and_cover_shell_edit_and_write(host):
    entries = _REAL_NAMES[host]
    assert all(e["name"] and e["source"].strip() and e["version"].strip() for e in entries)
    assert {e["kind"] for e in entries} <= {"shell", "edit", "write"}
    assert {"shell", "edit"} <= {e["kind"] for e in entries}


@pytest.mark.parametrize("host", ["grok", "agy", "codex"])
def test_real_tool_names_are_selected_by_the_written_matcher(host, tmp_path):
    matcher = _written_matcher(host, tmp_path)
    missing = [e["name"] for e in _REAL_NAMES[host] if not re.fullmatch(matcher, e["name"])]
    assert not missing, f"{host} matcher {matcher!r} does not select real tool names {missing}"


def test_real_tool_grok_matcher_is_not_a_wildcard(tmp_path):
    matcher = _written_matcher("grok", tmp_path)
    assert not re.fullmatch(matcher, "web_search") and not re.fullmatch(matcher, "grep")


def _plain_main(tmp_path, argv, **kw):
    hooks = tmp_path / "g" / "hooks.json"
    buf = io.StringIO()
    rc = _REAL_MAIN(argv, version_text="2.1.316", dest_dir=tmp_path / "d", out=buf, hooks_path=hooks, **kw)
    return rc, buf.getvalue(), hooks.parent


def _host_files(gdir):
    return sorted(p.name for p in gdir.iterdir()) if gdir.exists() else []


def test_plain_install_seeds_only_the_relay(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    rc, out, gdir = _plain_main(tmp_path, ["--install"], sac_blocked=lambda: False)
    assert rc == 0 and f"installed {len(ui.VENDORED_SCRIPTS)} scripts" in out
    assert _host_files(gdir) == []
    assert not (tmp_path / "home" / ".codex").exists() and not (tmp_path / "home" / ".gemini").exists()
    assert "login agent" not in out.replace("legacy login agent", "") and "allow rule" not in out
    assert "hook" not in out.lower()


def test_plain_install_registers_no_login_agent_and_autolink_flag_does(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    rc, out, gdir = _plain_main(tmp_path, ["--install"])
    assert rc == 0 and not (gdir / "login-agent").exists()
    rc, out, gdir = _plain_main(tmp_path, ["--install", "--autolink"])
    assert rc == 0 and "login agent created" in out and len(list((gdir / "login-agent").iterdir())) == 1


def test_plain_install_hosts_opt_in_seeds_named_hosts_and_never_grok_unnamed(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    rc, out, gdir = _plain_main(tmp_path, ["--install", "--hosts", "codex,agy"], sac_blocked=lambda: False)
    assert "codex hook" in out and "agy hook" in out and "agy allow rule" in out
    assert "grok" not in out and not (gdir / "grok-hooks.json").exists() and not (gdir / "grok-config.toml").exists()
    assert (gdir / "codex-hooks.json").exists() and (gdir / "agy-settings.json").exists()
    assert "login agent" not in out
    rc, out, gdir = _plain_main(tmp_path, ["--install", "--hosts", "grok"])
    assert "grok hook" in out and (gdir / "grok-hooks.json").exists()


def test_plain_install_agy_allow_rule_skipped_when_forwarder_is_sac_blocked(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    rc, out, gdir = _plain_main(tmp_path, ["--install", "--hosts", "agy"], sac_blocked=lambda: True)
    assert "agy allow rule skipped: Smart App Control" in out and "agy allow rule granted" not in out
    settings = gdir / "agy-settings.json"
    assert not settings.exists() or "coordinator-invoke" not in settings.read_text(encoding="utf-8")
    assert not (gdir / ".coordinator-allow-rule-agy.json").exists()
    rc, out, gdir = _plain_main(tmp_path, ["--install", "--hosts", "agy"], sac_blocked=lambda: False)
    assert "agy allow rule granted" in out


def test_plain_install_uninstall_without_hosts_still_removes_an_earlier_default_install(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    _home(monkeypatch, tmp_path)
    _plain_main(tmp_path, ["--install", "--hosts", "codex,agy,grok"], sac_blocked=lambda: False)
    gdir = tmp_path / "g"
    assert "PreToolUse" in (gdir / "grok-hooks.json").read_text(encoding="utf-8")
    buf = io.StringIO()
    assert _REAL_MAIN(["--uninstall"], out=buf, hooks_path=gdir / "hooks.json", dest_dir=tmp_path / "d") == 0
    out = buf.getvalue()
    for label in ("grok hook", "agy allow rule", "codex hook", "login agent"):
        assert label in out
    assert "PreToolUse" not in (gdir / "grok-hooks.json").read_text(encoding="utf-8")


def test_plain_install_help_states_the_new_default(capsys):
    with pytest.raises(SystemExit):
        _REAL_MAIN(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "--install seeds only the relay and no host unless named" in text and "--autolink" in text


class _FakeSacKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_winreg(value=None, error=None):
    mod = type(sys)("winreg")
    mod.HKEY_LOCAL_MACHINE = object()

    def open_key(*_a):
        if error is not None:
            raise error
        return _FakeSacKey()

    mod.OpenKey = open_key
    mod.QueryValueEx = lambda _k, _n: (value, 4)
    return mod


@pytest.mark.parametrize("value,error,expected", [
    (1, None, True),
    (2, None, False),
    (0, None, False),
    (None, FileNotFoundError(), False),
])
def test_launcher_sac_blocked_reads_the_registry_policy_state(monkeypatch, value, error, expected):
    monkeypatch.setattr(ui.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(value, error))
    assert ui.launcher_sac_blocked() is expected


def test_launcher_sac_blocked_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(ui.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(1))
    assert ui.launcher_sac_blocked() is False
