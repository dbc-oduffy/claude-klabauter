"""Tests for unimatrix-install.py: version gate, install copy, hooks, peer doctrine, platform parity."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-install.py"
_spec = importlib.util.spec_from_file_location("unimatrix_install", _SRC)
ui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui)

PLATFORMS = ["win32", "darwin", "linux"]


@pytest.fixture(autouse=True)
def _engine(monkeypatch, tmp_path):
    root = tmp_path / "engine"
    root.mkdir()
    monkeypatch.setattr(ui, "resolve_engine_root", lambda: (str(root), "published-target"))
    return root


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
    )


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
