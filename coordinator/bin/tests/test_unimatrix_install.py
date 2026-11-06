"""Tests for unimatrix-install.py: version gate, install copy, hooks, peer doctrine, platform parity."""
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-install.py"
_spec = importlib.util.spec_from_file_location("unimatrix_install", _SRC)
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

PLATFORMS = ["win32", "darwin", "linux"]
_REAL_RESOLVE = ui.resolve_engine_root


@pytest.fixture(autouse=True)
def _engine(monkeypatch, tmp_path):
    root = tmp_path / "engine"
    root.mkdir()
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
def _no_real_scheduler(monkeypatch):
    monkeypatch.setattr(ui, "run_schtasks", lambda *a: None)
    fake = _FakeWinreg()
    monkeypatch.setattr(ui, "_winreg", lambda: fake)


def _run(argv, text, dest=None, hooks=None):
    buf = io.StringIO()
    rc = ui.main(argv, version_text=text, dest_dir=dest, out=buf, hooks_path=hooks)
    return rc, buf.getvalue()


def _stub_src(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for n in ui.VENDORED_SCRIPTS:
        (src / n).write_text("# stub\n", encoding="utf-8")
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
        "unimatrix-peer-codex.py",
        "unimatrix-peer-agy.py",
        "unimatrix-agy-leg.py",
        "unimatrix-peer-router.py",
        "unimatrix-agy-hook-pretool.py",
        "unimatrix-agy-hook-launch.py",
        "unimatrix-agy-hook-sessionstart.py",
        "unimatrix-codex-hook-sessionstart.py",
        "unimatrix-host-guard.py",
        "unimatrix-doctrine-seed.py",
        "unimatrix-autolink.py",
        "unimatrix-liveness.py",
        "unimatrix-mirror-all.py",
        "unimatrix-reshape.py",
        "unimatrix-membership.py",
        "unimatrix-doctor.py",
        "unimatrix-pair.py",
    )


def test_publish_list_names_the_membership_scripts():
    text = (ui._BIN_DIR.parents[1] / "setup" / "publish-targets.portable").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("claude-klabauter-coordinator-bin|"))
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
    for n in ui.VENDORED_SCRIPTS:
        (src / n).write_text("# stub\n", encoding="utf-8")
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
    line = ui.authorized_keys_line("/home/a b/unimatrix", interpreter="/usr/bin/python3")
    assert line.startswith('restrict,command="/usr/bin/python3 \\"')
    assert 'unimatrix-relay.py\\"" ssh-ed25519' in line


def test_macos_prefers_homebrew_python_over_stub(monkeypatch, tmp_path):
    brew = tmp_path / "brew-python3"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    monkeypatch.setattr(ui, "HOMEBREW_PYTHONS", (str(tmp_path / "absent"), str(brew)))
    monkeypatch.setattr(sys, "platform", "darwin")
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
    for n in ui.VENDORED_SCRIPTS:
        (src / n).write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(ui, "__file__", str(src / "unimatrix-install.py"))
    dest = tmp_path / "unimatrix"
    rc, out = _run(["--install", "--force"], "2.2.0", dest, tmp_path / "h" / "hooks.json")
    assert rc == 0 and "WARN" in out and dest.is_dir()


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


def test_probe_missing_claude(monkeypatch):
    monkeypatch.setattr(ui.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="claude not found"):
        ui.probe_claude_version()


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
def test_trust_codex_hook_marks_hook_trusted_in_real_codex(tmp_path):
    hp = tmp_path / "hooks.json"
    ui.seed_codex_hook(hp, tmp_path)
    ok, note = ui.trust_codex_hook(hp)
    assert ok, note
    cfg = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "trusted_hash" in cfg and "session_start" in cfg


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
    assert str(tmp_path / ui.SEND_SCRIPT) in rule and rule.count("prefix_rule") == 1
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
    assert guard["matcher"] == "Bash"
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
    assert "governance scope: shell-command" in out


def test_unresolved_engine_root_is_degraded_without_engine_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (None, "unresolved"))
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 0 and "governance DEGRADED: engine root unresolved" in out
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
    cmds = [("sessionStart", data["hooks"]["SessionStart"][0]["hooks"][0]["command"]),
            ("preToolUse", data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]),
            ("preToolUse", "theirs")]
    fake = _FakeCodex(chp, cmds, **kw)
    monkeypatch.setattr(ui.shutil, "which", lambda n: "/bin/codex")
    monkeypatch.setattr(ui.subprocess, "Popen", lambda *a, **k: fake)
    return ui.trust_codex_hook(chp), fake


def test_trust_covers_guard_and_skips_foreign(monkeypatch, tmp_path):
    (ok, note), fake = _trust_with(monkeypatch, tmp_path)
    assert ok, note
    assert [r.get("trustedHash") for r in fake.rows] == ["h0", "h1", None]


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
    monkeypatch.setattr(ui, "_BIN_DIR", bin_dir)
    home, rc, out = _default_install(monkeypatch, tmp_path)
    assert rc == 0 and "governance DEGRADED: engine root unresolved (resolver failed: no engine root)" in out
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


def test_macos_login_agent_is_run_at_load_once_with_no_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    dest, agents = tmp_path / "unimatrix", tmp_path / "LaunchAgents"
    assert ui.register_login_agent(dest, agents)[0] == "created"
    [plist] = list(agents.iterdir())
    import plistlib
    spec = plistlib.loads(plist.read_bytes())
    assert plist.name == f"{ui.LOGIN_AGENT_LABEL}.plist" and spec["Label"] == ui.LOGIN_AGENT_LABEL
    assert spec["RunAtLoad"] is True
    assert not {"KeepAlive", "StartInterval", "StartCalendarInterval", "WatchPaths"} & set(spec)
    assert spec["ProgramArguments"] == [sys.executable, str(dest / ui.AUTOLINK_SCRIPT)]
    assert spec["ProgramArguments"][1].startswith(str(tmp_path))


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
    """Fake subprocess.run for --pair: ssh -G, _machine_local get and set, ssh-keygen."""

    def __init__(self, store=None):
        self.store, self.calls = dict(store or {}), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        done = lambda rc=0, out="": type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()
        if argv[0] == "ssh":
            return done(0, "")
        if argv[0] == "ssh-keygen":
            path = Path(argv[argv.index("-f") + 1])
            path.write_text("PRIVATE", encoding="utf-8")
            Path(str(path) + ".pub").write_text("ssh-ed25519 AAAAPAIRBODY unimatrix\n", encoding="utf-8")
            return done()
        if argv[2] == "get":
            return done(0, self.store[argv[3]]) if argv[3] in self.store else done(1)
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
    priv, body = ui.pair_key("far-1", run=box)
    assert priv == home / ".secrets" / "ssh" / "unimatrix-far-1" and body == "AAAAPAIRBODY"
    assert box.store == {"unimatrix.keys.far-1": str(priv)}
    keygens = [c for c in box.calls if c[0] == "ssh-keygen"]
    assert len(keygens) == 1 and "-N" in keygens[0] and keygens[0][keygens[0].index("-N") + 1] == ""
    priv2, body2 = ui.pair_key("far-1", run=box)
    assert (priv2, body2) == (priv, body) and len([c for c in box.calls if c[0] == "ssh-keygen"]) == 1


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
    rc = ui.main(["--pair", "x"], out=buf, pair_run=_PairBox())
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
    assert status == "admin" and seen and "C:\\Python312\\python.exe" in line
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


def test_hosts_writes_only_named_host_paths(monkeypatch, tmp_path):
    home, rc, _ = _default_install(monkeypatch, tmp_path, ("--install", "--hosts", "codex"))
    assert rc == 0 and (home / ".codex").exists()
    assert not (home / ".gemini").exists()
    home2 = tmp_path / "home2"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home2))
    rc = ui.main(["--install", "--hosts", "agy"], version_text="2.1.316", dest_dir=tmp_path / "dest", out=io.StringIO())
    assert rc == 0 and (home2 / ".gemini").exists()
    assert not (home2 / ".codex").exists() and not (home2 / ".agents").exists()
