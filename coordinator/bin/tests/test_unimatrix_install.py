"""Tests for unimatrix-install.py: version gate, install copy, hooks, peer doctrine, platform parity."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-install.py"
_spec = importlib.util.spec_from_file_location("unimatrix_install", _SRC)
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

PLATFORMS = ["win32", "darwin", "linux"]
_REAL_RESOLVE = ui.resolve_engine_root
_REAL_AGY_PROBE = ui.agy_resolves_bare_invoke


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
def _no_real_agy_login_shell_probe(monkeypatch):
    monkeypatch.setattr(ui, "agy_resolves_bare_invoke", lambda: True)


@pytest.fixture(autouse=True)
def _no_real_launchctl_ps_or_pin(monkeypatch):
    monkeypatch.setattr(ui, "run_launchctl", lambda *a: None)
    monkeypatch.setattr(ui._lib("unimatrix_relay_sessions"), "ps_snapshot", lambda *a, **k: "")
    monkeypatch.setattr(ui, "_pin_value", lambda: None)
    monkeypatch.setattr(ui, "_pin_raw", lambda: "/stub/pin")  # abs-path-ok: set pin; record_python_pin writes nothing
    monkeypatch.setattr(ui, "_write_pin", lambda interp: pytest.fail("machine-local write outside a pin test"))


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
    assert entry["matcher"] == "run_command"
    cmd = _hook_cmd(hp)
    assert sys.executable in cmd and str(dest / ui.AGY_HOOK_SCRIPT) in cmd
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
    assert entry["PreToolUse"][0]["matcher"] == "run_command"
    handler = entry["SessionStart"][0]
    # agy's SessionStart is flat: a grouped entry makes agy reject the whole hooks.json.
    assert handler["type"] == "command" and "hooks" not in handler and "matcher" not in handler
    assert str(tmp_path / ui.AGY_SESSIONSTART_SCRIPT) in handler["command"] and sys.executable in handler["command"]


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
    assert str(tmp_path / ui.CODEX_SESSIONSTART_SCRIPT) in groups[1]["hooks"][0]["command"]


def test_codex_seed_creates_file_and_unseed_restores_foreign(tmp_path):
    hp = tmp_path / "codex" / "hooks.json"
    assert ui.seed_codex_hook(hp, tmp_path) == "created"
    assert ui.unseed_codex_hook(hp) == "removed"
    assert json.loads(hp.read_text(encoding="utf-8")) == {}
    assert ui.unseed_codex_hook(hp) == "absent"
    foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "theirs"}]}]}}
    hp.write_text(json.dumps(foreign), encoding="utf-8")
    ui.seed_codex_hook(hp, tmp_path)
    assert ui.unseed_codex_hook(hp) == "removed"
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
    assert str(dest / ui.CODEX_SESSIONSTART_SCRIPT) in cmd


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
    rc = ui.main(["--uninstall"], out=buf, hooks_path=tmp_path / "h.json", codex_hooks=chp)
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
    ok, note = ui.trust_codex_hook(hp)
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
    monkeypatch.setattr(ui, "trust_codex_hook", lambda p: (True, "x"))
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
        assert str(dest / ui.SEND_SCRIPT) in text and "{SEND}" not in text
        assert "--to <sender>" in text and "--list" in text


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
    assert str(tmp_path / "dest" / ui.SEND_SCRIPT) in text
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
    assert json.dumps(str(tmp_path / ui.SEND_SCRIPT)) in rule and rule.count("prefix_rule") == 1
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
    assert "unimatrix-host-guard.py" in handler["command"] and "--host codex" in handler["command"]
    assert str(_engine) in handler["command"] and isinstance(handler["timeout"], int)
    agy = _hooks_file(home / ".gemini" / "config" / "hooks.json")[ui.AGY_HOOK_KEY]
    assert len(agy["PreToolUse"]) == 1 and str(_engine) in agy["PreToolUse"][0]["hooks"][0]["command"]
    assert "hooks" not in agy["SessionStart"][0] and agy["SessionStart"][0]["type"] == "command"
    for rules in (home / ".codex" / "AGENTS.md", home / ".gemini" / "GEMINI.md"):
        text = rules.read_text(encoding="utf-8")
        assert ui.DOCTRINE_BEGIN in text and "<!-- BEGIN coordinator-doctrine" in text
    skills = _hooks_file(home / ".gemini" / "config" / "skills.json")
    assert skills["entries"] == [{"path": str(ui.SKILLS_ROOT)}]
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
    assert ui.main(["--uninstall"], out=buf) == 0
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
    assert len(codex["PreToolUse"]) == 1 and str(other) in codex["PreToolUse"][0]["hooks"][0]["command"]
    assert "codex hook updated" in buf.getvalue()


def test_no_claude_on_path_hosts_skip_gate_but_bare_install_refuses(monkeypatch, tmp_path):
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
    ((matcher, command),) = _grok_guard_commands(home / ".grok" / "hooks.json")
    assert "unimatrix-host-guard.py" in command and "--host grok" in command
    for tool in ("Bash", "run_command", "Edit", "Write", "MultiEdit", "NotebookEdit", "edit_file", "write_file"):
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
    assert "unimatrix-host-guard.py" in command and "--host grok" in command
    # codex's own guard entry is a different host's and is not the grok one
    assert "--host codex" in chp.read_text(encoding="utf-8")


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
    return ui.trust_codex_hook(chp), fake


def test_trust_covers_guard_and_skips_foreign(monkeypatch, tmp_path):
    (ok, note), fake = _trust_with(monkeypatch, tmp_path)
    assert ok, note
    assert len([r for r in fake.rows if r.get("trustedHash")]) == 11
    assert all(r.get("trustedHash") == r["currentHash"] for r in fake.rows if r["command"] != "theirs")
    assert next(r for r in fake.rows if r["command"] == "theirs").get("trustedHash") is None


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
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(root))
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 0 and f"governance engine root: {root} (settings-home ladder)" in out
    assert "governance DEGRADED" not in out
    codex = _hooks_file(home / ".codex" / "hooks.json")["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    agy = _hooks_file(home / ".gemini" / "config" / "hooks.json")[ui.AGY_HOOK_KEY]["PreToolUse"][0]["hooks"][0]["command"]
    assert f"--engine-root {root}" in codex and f"--engine-root {root}" in agy


def test_degraded_line_names_the_resolver_failure_reason(monkeypatch, tmp_path):
    bin_dir = tmp_path / "fakebin"
    (bin_dir / "lib").mkdir(parents=True)
    (bin_dir / "lib" / "cc_invoke.py").write_text(
        "def resolve_engine_root(script_file):\n    raise RuntimeError('no engine root\\nfix it')\n",
        encoding="utf-8")
    monkeypatch.setattr(ui, "resolve_engine_root", _REAL_RESOLVE)
    monkeypatch.setattr(ui, "PLUGIN_ROOT", tmp_path / "std-doctrine" / "coordinator")
    # _seed() caches the doctrine-seed module from _BIN_DIR; load the real one before _BIN_DIR points at
    # fakebin, or this test passes only when an earlier test happened to load it.
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


def test_an_existing_pin_is_never_overwritten_even_when_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "_pin_raw", lambda: str(tmp_path / "gone-python3"))
    assert ui.record_python_pin(tmp_path) is None  # autouse _write_pin fails the test on any write


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
    monkeypatch.setattr(sys, "platform", "linux")
    units = tmp_path / "systemd" / "user"
    assert ui.register_login_agent(tmp_path / "u", units)[0] == "created"
    unit = (units / ui.LOGIN_UNIT).read_text(encoding="utf-8")
    assert "Type=oneshot" in unit and str(tmp_path / "u" / ui.AUTOLINK_SCRIPT) in unit
    assert "Restart" not in unit and "Timer" not in unit
    link = units / "default.target.wants" / ui.LOGIN_UNIT
    assert link.is_symlink() and link.resolve() == (units / ui.LOGIN_UNIT).resolve()
    assert ui.register_login_agent(tmp_path / "u", units)[0] == "present"
    assert ui.unregister_login_agent(units) == "removed"
    assert not (units / ui.LOGIN_UNIT).exists() and not link.is_symlink()


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


class _PairBox:
    """Fake subprocess.run for --pair: ssh -G, _machine_local get and set, ssh-keygen, and the 1Password `op` CLI."""

    def __init__(self, store=None, op_missing=False, items=None, vault="Vault"):
        self.store, self.calls = dict(store or {}), []
        self.op_missing, self.items, self.vault = op_missing, dict(items or {}), vault
        self.fail_edit = False

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
        if argv[1:3] == ["item", "delete"]:
            return done() if self.items.pop(argv[3], None) is not None else done(1, "", "isn't an item")
        if argv[1:3] == ["item", "edit"]:
            if self.fail_edit:
                return done(1, "", "boom")
            title = next(a for a in argv if a.startswith("--title="))[len("--title="):]
            if title in self.items:
                raise AssertionError("retitle would collapse two items")
            self.items[title] = self.items.pop(argv[3])
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


def test_pair_default_mints_a_passphrase_encrypted_key_from_the_1password_item(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox()
    refs = []
    priv, body = ui.pair_key("far-1", run=box, on_mint=refs.append)
    assert priv == home / ".secrets" / "ssh" / "unimatrix-far-1" and _passphrase().is_encrypted(priv)
    assert refs == ["op://Vault/unimatrix-far-1/password"] and box.items == {"unimatrix-far-1": "generated-pass-unimatrix-far-1"}
    assert box.store == {"unimatrix.keys.far-1": str(priv)}
    assert not [c for c in box.calls if c[0] == "ssh-keygen"]
    secret = box.items["unimatrix-far-1"]
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


def test_pair_remint_removes_old_pair_and_item_then_mints_encrypted(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox(items={"unimatrix-far-1": "old-secret"})
    key = home / ".secrets" / "ssh" / "unimatrix-far-1"
    key.parent.mkdir(parents=True)
    key.write_text("OLD", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 OLDBODY c\n", encoding="utf-8")
    box.store["unimatrix.keys.far-1"] = str(key)
    refs = []
    priv, body = ui.pair_key("far-1", run=box, remint=True, on_mint=refs.append)
    assert priv == key and body != "OLDBODY" and _passphrase().is_encrypted(key)
    assert box.items == {"unimatrix-far-1": "generated-pass-unimatrix-far-1.new"}
    assert refs == ["op://Vault/unimatrix-far-1/password"]
    assert not list(key.parent.glob("*.new*"))
    ops = [(c[2], next((a for a in c if a.startswith("--title=")), c[3])) for c in box.calls
           if c[0] == "op" and c[1] == "item"]
    assert ops == [("create", "--title=unimatrix-far-1.new"), ("delete", "unimatrix-far-1"),
                   ("edit", "--title=unimatrix-far-1")]


def test_pair_remint_writes_new_key_and_item_before_deleting_the_superseded_ones(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox(items={"unimatrix-far-1": "old-secret"})
    key = home / ".secrets" / "ssh" / "unimatrix-far-1"
    key.parent.mkdir(parents=True)
    key.write_text("OLD", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 OLDBODY c\n", encoding="utf-8")
    box.store["unimatrix.keys.far-1"] = str(key)
    mod = _passphrase()
    seen = []
    real_mint, real_delete = mod.mint_key, mod.delete_item
    monkeypatch.setattr(mod, "mint_key", lambda p, c, pw: (seen.append(("mint", Path(p).name,
                        key.read_text(encoding="utf-8"), dict(box.items))), real_mint(p, c, pw))[1])
    monkeypatch.setattr(mod, "delete_item", lambda a, run=None: (seen.append(("delete", a,
                        _passphrase().is_encrypted(key), dict(box.items))), real_delete(a, run))[1])
    ui.pair_key("far-1", run=box, remint=True)
    assert seen[0][:3] == ("mint", "unimatrix-far-1.new", "OLD")
    assert set(seen[0][3]) == {"unimatrix-far-1", "unimatrix-far-1.new"}
    assert seen[1][:3] == ("delete", "far-1", True) and set(seen[1][3]) == {"unimatrix-far-1", "unimatrix-far-1.new"}


def test_pair_remint_failing_before_the_new_key_is_written_keeps_the_superseded_key_and_item(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox(items={"unimatrix-far-1": "old-secret"})
    key = home / ".secrets" / "ssh" / "unimatrix-far-1"
    key.parent.mkdir(parents=True)
    key.write_text("OLD", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 OLDBODY c\n", encoding="utf-8")
    box.store["unimatrix.keys.far-1"] = str(key)
    mod = _passphrase()

    def boom(p, c, pw):
        Path(p).write_text("PARTIAL", encoding="utf-8")
        raise RuntimeError("mint failed")

    monkeypatch.setattr(mod, "mint_key", boom)
    with pytest.raises(RuntimeError, match="mint failed"):
        ui.pair_key("far-1", run=box, remint=True)
    assert key.read_text(encoding="utf-8") == "OLD" and Path(str(key) + ".pub").is_file()
    assert box.items == {"unimatrix-far-1": "old-secret"} and not list(key.parent.glob("*.new*"))


def test_pair_remint_with_locked_1password_keeps_the_superseded_key_and_item(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    box = _PairBox(items={"unimatrix-far-1": "old-secret"})
    key = home / ".secrets" / "ssh" / "unimatrix-far-1"
    key.parent.mkdir(parents=True)
    key.write_text("OLD", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 OLDBODY c\n", encoding="utf-8")
    box.store["unimatrix.keys.far-1"] = str(key)
    mod = _passphrase()

    def locked(alias, run=None):
        raise mod.OnePasswordLocked("1password_locked: test")

    monkeypatch.setattr(mod, "ensure_item", locked)
    with pytest.raises(mod.OnePasswordLocked):
        ui.pair_key("far-1", run=box, remint=True)
    assert key.read_text(encoding="utf-8") == "OLD" and Path(str(key) + ".pub").is_file()
    assert box.items == {"unimatrix-far-1": "old-secret"}


def _remint_box(home, monkeypatch):
    box = _PairBox(items={"unimatrix-far-1": "old-secret"})
    key = home / ".secrets" / "ssh" / "unimatrix-far-1"
    key.parent.mkdir(parents=True)
    key.write_text("OLD", encoding="utf-8")
    Path(str(key) + ".pub").write_text("ssh-ed25519 OLDBODY c\n", encoding="utf-8")
    box.store["unimatrix.keys.far-1"] = str(key)
    return box, key


def test_pair_remint_failing_to_delete_the_superseded_item_names_the_new_item_and_remint(monkeypatch, tmp_path):
    box, key = _remint_box(_pair_home(monkeypatch, tmp_path), monkeypatch)
    mod = _passphrase()

    def locked(alias, run=None):
        raise mod.OnePasswordLocked("1password_locked: test")

    monkeypatch.setattr(mod, "delete_item", locked)
    with pytest.raises(RuntimeError, match=r"new key is in place.*unimatrix-far-1\.new.*re-run --remint"):
        ui.pair_key("far-1", run=box, remint=True)
    assert mod.is_encrypted(key)


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


def test_file_key_default_is_encrypted_and_remint_with_no_op_refuses(monkeypatch, tmp_path):
    home = _pair_home(monkeypatch, tmp_path)
    buf = io.StringIO()
    box = _PairBox()
    assert ui.main(["--file-key"], out=buf, pair_run=box) == 0
    assert "passphrase: op://Vault/unimatrix-file/password" in buf.getvalue()
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


def test_recorded_pair_keys_refuses_a_registry_read_exiting_other_than_0_or_1(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    done = lambda rc, err="": type("R", (), {"returncode": rc, "stdout": "", "stderr": err})()
    with pytest.raises(RuntimeError, match="unimatrix.far_boxes failed: boom"):
        ui.recorded_pair_keys(run=lambda argv, **kw: done(2, "boom"))
    assert ui.recorded_pair_keys(run=lambda argv, **kw: done(1)) == {}


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


def test_authorized_keys_line_key_body_with_posix_and_windows_interpreters():
    posix = ui.authorized_keys_line("/home/me/unimatrix", "/usr/bin/python3", "KB")
    win = ui.authorized_keys_line(r"\Users\me\unimatrix", r"\Python312\python.exe", "KB")
    assert posix.endswith(" ssh-ed25519 KB unimatrix") and win.endswith(" ssh-ed25519 KB unimatrix")
    assert "/usr/bin/python3 " in posix and "python.exe " in win
    assert "<key-body>" in ui.authorized_keys_line("/d", "/p")


class _FarBox(_PairBox):
    """_PairBox whose ssh runs the stdin program locally under a fake HOME, as the far box would."""

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
        r = subprocess.run([sys.executable, "-"], input=kw["input"], capture_output=True, text=True, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done(r.returncode, r.stdout)


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


def test_pair_main_reports_added_then_present(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    box = _FarBox(tmp_path / "far")
    first, second = io.StringIO(), io.StringIO()
    assert ui.main(["--pair", "x"], out=first, pair_run=box) == 0
    assert ui.main(["--pair", "x"], out=second, pair_run=box) == 0
    assert "added on x" in first.getvalue() and "already present on x" in second.getvalue()
    assert len((tmp_path / "far" / ".ssh" / "authorized_keys").read_text(encoding="utf-8").splitlines()) == 1


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
    assert _hooks_file(home / ".gemini" / "config" / "skills.json")["entries"] == [{"path": str(plugin / "skills")}]
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
    assert rc == 0 and (home / ".gemini" / "config" / "skills.json").exists()
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
    assert ui.seed_agy_plugin(pj, tree) == "created"
    assert ui.seed_agy_plugin(pj, tree) == "present"
    data = json.loads(pj.read_text(encoding="utf-8"))
    assert data["entries"] == [{"path": str(tree.parent), "include_only": ["agy-plugin"]}]
    data["entries"].insert(0, {"path": "/mine"})
    data["inherits"] = [{"path": "/shared.json"}]
    pj.write_text(json.dumps(data), encoding="utf-8")
    assert ui.seed_agy_plugin(pj, tree) == "present"
    assert ui.seed_agy_plugin(pj, tmp_path / "other" / "agy-plugin") == "added"
    after = json.loads(pj.read_text(encoding="utf-8"))
    assert after["entries"][0] == {"path": "/mine"} and after["inherits"] == [{"path": "/shared.json"}]
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
    assert "--event SessionStart" in start[1]["command"]
    end = events["SessionEnd"]["hooks"][0]
    assert "--event SessionEnd" in end["command"]
    assert end["timeout"] <= 3 and start[1]["timeout"] <= 3
    assert str(tmp_path / "published engine") in end["command"]


def test_codex_install_and_remove_preserve_foreign_handlers_inside_owned_group(tmp_path):
    hp = tmp_path / "hooks.json"
    foreign = {"type":"command", "command":"foreign-inside-group"}
    old = ui.codex_hook_events(tmp_path)["SessionStart"]
    old["hooks"].append(foreign)
    hp.write_text(json.dumps({"hooks":{"SessionStart":[old]}}), encoding="utf-8")
    ui.seed_codex_hook(hp, tmp_path)
    data = _hooks_file(hp)
    assert data["hooks"]["SessionStart"][0]["hooks"] == [foreign]
    assert len(ui._codex_expected_handlers(hp)) == 11
    assert ui.seed_codex_hook(hp, tmp_path) == "present"
    ui.unseed_codex_hook(hp)
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
    proc = subprocess.run([sys.executable, str(script), "--engine-root", str(engine), "--event", event],
                          input=json.dumps({"session_id":"parent-session", **({"agent_id":"child"} if event == "SubagentStart" else {})}), text=True, capture_output=True,
                          timeout=3, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert proc.returncode == 0, proc.stderr
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
    assert proc.returncode == 0 and json.loads(proc.stdout) == {}
    assert "lacks session.external_registry" in proc.stderr
    assert "AMBIENT MODULE LOADED" not in proc.stderr


def test_codex_advisory_events_use_one_bounded_owned_handler(tmp_path):
    events = ui.codex_hook_events(tmp_path, tmp_path / 'engine')
    for event in ('UserPromptSubmit','PostToolUse','Stop','SubagentStop','PreCompact','PostCompact'):
        group = events[event]
        assert len(group['hooks']) == 1
        hook = group['hooks'][0]
        assert hook['timeout'] <= 3
        assert '--event '+event in hook['command']
    assert events['PreToolUse']['matcher'] == '.*'


@pytest.mark.parametrize('before', ['', 'model = "gpt-test"\n[tui]\ntheme = "custom"\n', 'tui = { theme = "custom" }\n'])
def test_codex_footer_default_is_idempotent_and_uninstall_owned(tmp_path,before):
    cfg=tmp_path/'config.toml';cfg.write_text(before,encoding='utf-8')
    original=tomllib.loads(before)
    assert ui.seed_codex_status_line(cfg)=='updated'
    parsed=tomllib.loads(cfg.read_text())
    assert parsed['tui']['status_line']==['thread-name','model-with-reasoning','context-remaining']
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
    assert parsed['tui']['status_line']==['git-branch','thread-name','five-hour-limit','model','reasoning','context-remaining']
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
    assert parsed['tui']['status_line']==['thread-name','weekly-limit','model-with-reasoning','context-remaining']


def test_codex_footer_invalid_toml_never_overwritten(tmp_path):
    cfg=tmp_path/'config.toml';cfg.write_text('[tui\n')
    with pytest.raises(ValueError):ui.seed_codex_status_line(cfg)
    assert cfg.read_text()=='[tui\n'


def test_codex_footer_is_automatic_in_install_and_owned_uninstall(monkeypatch,tmp_path):
    _stub_src(monkeypatch,tmp_path)
    home=_home(monkeypatch,tmp_path)
    cfg=home/'.codex'/'config.toml';cfg.parent.mkdir(parents=True)
    before='model="keep"\n[tui]\nstatus_line=["current-dir","git-branch"]\n'
    cfg.write_text(before)
    output=io.StringIO()
    assert ui.main(['--install','--hosts','codex'], version_text='2.1.316',dest_dir=tmp_path/'dest',out=output)==0
    assert 'codex status footer updated' in output.getvalue()
    assert tomllib.loads(cfg.read_text())['tui']['status_line']==['thread-name','git-branch','model-with-reasoning','context-remaining']
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
    assert data["statusLine"]["command"].endswith("-m coordinator_core.tui_status agy")
    parsed = tomllib.loads(grok.read_text(encoding="utf-8"))
    assert parsed["model"] == "x" and parsed["mcp"]["foreign"] == {"url": "u"}
    assert parsed["ui"]["status_line"]["type"] == "command"
    assert parsed["ui"]["status_line"]["command"].endswith("-m coordinator_core.tui_status grok")
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
    assert agy_data["statusLine"] == {"command": "my-line"} and agy_data["permissions"]["allow"] == [ui.AGY_ALLOW_RULE]
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
    assert allow == ["command(coordinator-invoke engine_ask.submit --params-file .coordinator-local/em-submit.json)"]
    assert not any("*" in rule for rule in allow)


def test_agy_allow_rule_installs_once_keeps_operator_entries_and_uninstall_restores_bytes(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    mine = "command(Get-ChildItem ~/x)"
    before = json.dumps({"permissions": {"allow": [mine]}, "theme": "dark"}).encode("utf-8")
    agy.write_bytes(before)
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and "agy allow rule updated" in out
    allow = json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == [mine, ui.AGY_ALLOW_RULE]
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
    assert allow == [mine, ui.AGY_ALLOW_RULE]
    once = agy.read_bytes()
    rc, out = _tui_main(tmp_path, "--install")
    assert "agy allow rule present" in out and agy.read_bytes() == once
    rc, out = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and agy.read_bytes() == before

    agy.write_bytes(json.dumps({"permissions": {"allow": [mine, legacy]}}).encode("utf-8"))
    _tui_main(tmp_path, "--install")
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine, legacy, ui.AGY_ALLOW_RULE]
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


def test_agy_allow_rule_bare_resolves_grants_bare_line_and_reports_it(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    agy.parent.mkdir(parents=True)
    rc, out = _tui_main(tmp_path, "--install")
    assert rc == 0 and f"agy allow rule granted: {ui.AGY_ALLOW_RULE}" in out
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [ui.AGY_ALLOW_RULE]


def test_agy_allow_rule_bare_unresolved_grants_absolute_line_migrates_and_uninstalls(monkeypatch, tmp_path):
    _, agy, _ = _tui_home(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "agy_resolves_bare_invoke", lambda: False)
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
    monkeypatch.setattr(ui, "agy_resolves_bare_invoke", lambda: True)
    _tui_main(tmp_path, "--install")
    assert json.loads(agy.read_text(encoding="utf-8"))["permissions"]["allow"] == [mine, ui.AGY_ALLOW_RULE]
    monkeypatch.setattr(ui, "agy_resolves_bare_invoke", lambda: False)
    _tui_main(tmp_path, "--install")
    rc, _ = _tui_main(tmp_path, "--uninstall")
    assert rc == 0 and agy.read_bytes() == before


def test_agy_resolves_bare_invoke_reads_login_shell_probe(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, fake_run.rc, stdout=fake_run.out, stderr="")

    monkeypatch.setattr(ui, "agy_resolves_bare_invoke", _REAL_AGY_PROBE)
    monkeypatch.setattr(ui.subprocess, "run", fake_run)
    monkeypatch.setattr(ui.sys, "platform", "linux")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    fake_run.rc, fake_run.out = 0, "/usr/local/bin/coordinator-invoke\n"
    assert ui.agy_resolves_bare_invoke() is True
    assert calls == [["/bin/zsh", "-lc", "command -v coordinator-invoke"]]
    fake_run.rc, fake_run.out = 1, ""
    assert ui.agy_resolves_bare_invoke() is False

    def boom(argv, **kw):
        raise OSError("no shell")

    monkeypatch.setattr(ui.subprocess, "run", boom)
    assert ui.agy_resolves_bare_invoke() is False
    monkeypatch.setattr(ui.sys, "platform", "win32")
    monkeypatch.setattr(ui.shutil, "which", lambda name: "C:/x/coordinator-invoke.cmd")
    assert ui.agy_resolves_bare_invoke() is True
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    assert ui.agy_resolves_bare_invoke() is False


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


def test_control_key_mints_beside_ssh_and_prints_alias_block_and_control_line(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    fake_home = tmp_path / "userhome"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    buf = io.StringIO()
    box = _PairBox()
    assert ui.main(["--control-key", "machine-a", "--from-cidr", "10.0.0.0/8"], out=buf, pair_run=box) == 0
    text = buf.getvalue()
    key = fake_home / ".ssh" / "unimatrix_control_machine_a"
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
    assert rec == {"platform": sys.platform, "interpreter": sys.executable,
                   "script": os.path.join(str(far / "fsh"), "unimatrix", "unimatrix-control.py"), "shell": "posix"}
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


def test_membership_without_this_box_or_with_empty_cidrs_is_no_pin(monkeypatch, tmp_path):
    _pair_home(monkeypatch, tmp_path)
    membership = ui._sibling("unimatrix-membership.py")
    monkeypatch.setattr(ui._sibling("unimatrix-peer-contract.py"), "local_box", lambda: "me")
    for members in ([{"box": "other", "from_cidrs": ["1.1.1.0/24"]}], [{"box": "me", "from_cidrs": []}]):
        monkeypatch.setattr(membership, "verified_members", lambda home, run=None, m=members: m)
        with pytest.raises(ValueError, match="--from-cidr"):
            ui.control_from_cidrs(None)


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


def test_grok_hook_skipped_cleanly_when_grok_absent_and_seeded_once_grok_home_exists(monkeypatch, tmp_path):
    _stub_src(monkeypatch, tmp_path)
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "grok hook skipped" in buf.getvalue() and "grok hook created" not in buf.getvalue()
    assert not (home / ".grok" / "hooks.json").exists()
    (home / ".grok").mkdir(exist_ok=True)
    buf = io.StringIO()
    assert ui.main(["--install", "--hosts", "grok"], dest_dir=tmp_path / "d", out=buf) == 0
    assert "grok hook created" in buf.getvalue() and (home / ".grok" / "hooks.json").is_file()


def test_grok_hook_seeded_when_grok_binary_on_path(monkeypatch, tmp_path):
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
