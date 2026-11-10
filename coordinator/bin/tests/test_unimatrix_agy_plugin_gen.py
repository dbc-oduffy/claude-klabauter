"""Behavior pins for unimatrix-plugin-gen.py (table-driven plugin tree translation) and its installer wiring."""
import ast
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_BIN = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("unimatrix_plugin_gen", "unimatrix-plugin-gen.py")
ui = _load("unimatrix_install_for_gen", "unimatrix-install.py")

HOOKS = {
    "description": "top level string agy refuses",
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "description": "d",
                                           "command": "python ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/run.py"}]},
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "x"}]},
            {"matcher": "Weird", "hooks": [{"type": "command", "command": "y"}]},
        ],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "z"}]}],
    },
}


def _agent(name, model):
    return f"---\r\nname: {name}\r\nmodel: {model}\r\ntools: [Read]\r\n---\r\nbody ${{CLAUDE_PLUGIN_ROOT}}/x\r\n"


@pytest.fixture
def plugin(tmp_path):
    root = tmp_path / "src" / "plugin"
    for n, m in (("a-opus", "opus"), ("a-sonnet", "sonnet"), ("a-haiku", "haiku"), ("a-odd", "gpt-9")):
        (root / "agents").mkdir(parents=True, exist_ok=True)
        (root / "agents" / f"{n}.md").write_bytes(_agent(n, m).encode())
    (root / "commands" / "sub").mkdir(parents=True)
    (root / "commands" / "foo.md").write_bytes(b"---\r\ndescription: Foo\r\n---\r\nrun foo\r\n")
    (root / "commands" / "sub" / "bar.md").write_bytes(b"no frontmatter, no trailing newline")
    (root / "skills" / "real-dir").mkdir(parents=True)
    (root / "skills" / "real-dir" / "SKILL.md").write_bytes(b"---\nname: real-name\ndescription: R\n---\nbody\n")
    (root / "skills" / "real-dir" / "ref.txt").write_bytes(b"ref\r\n")
    (root / "skills" / "flat.md").write_bytes(b"flat skill\n")
    (root / "hooks" / "scripts").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text(json.dumps(HOOKS), encoding="utf-8")
    (root / "hooks" / "scripts" / "run.py").write_bytes(b"print(1)\r\n")
    (root / "extras").mkdir()
    (root / "extras" / "x.txt").write_bytes(b"x\n")
    (root.parent / "global-doctrine" / "rules").mkdir(parents=True)
    (root.parent / "global-doctrine" / "CLAUDE.md").write_bytes(b"# doctrine\r\n")
    (root.parent / "global-doctrine" / "rules" / "rules.md").write_bytes(b"- rule\n")
    return root


def _tree(out):
    found = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    return dict(sorted(found.items()))


def _unmapped(report):
    return {(u["kind"], u["source"]) for u in report["unmapped"]}


def test_tiers_translated_and_unknown_model_dropped_and_reported(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    t = _tree(out)
    assert b"model: pro\n" in t["agents/a-opus.md"]
    assert b"model: flash\n" in t["agents/a-sonnet.md"]
    assert b"model: flash_lite\n" in t["agents/a-haiku.md"]
    assert b"model:" not in t["agents/a-odd.md"]
    assert ("agent-model", "agents/a-odd.md") in _unmapped(report)
    assert b"${PLUGIN_ROOT}/x" in t["agents/a-opus.md"] and b"CLAUDE_PLUGIN_ROOT" not in t["agents/a-opus.md"]


def test_null_tier_drops_model_and_reports(plugin, tmp_path, monkeypatch):
    table = gen.load_table()
    table["agy"]["tiers"]["haiku"] = None
    monkeypatch.setattr(gen, "load_table", lambda: table)
    report = gen.generate(plugin, tmp_path / "out", "agy")
    assert b"model:" not in (tmp_path / "out" / "agents" / "a-haiku.md").read_bytes()
    assert ("agent-model", "agents/a-haiku.md") in _unmapped(report)


def test_hooks_at_root_schema_matchers_and_plugin_root(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    assert not (out / "hooks" / "hooks.json").exists()
    data = json.loads((out / "hooks.json").read_text(encoding="utf-8"))
    assert "description" not in data
    assert "hooks" not in data
    pre = data["pre-tool-use"]["PreToolUse"]
    assert [g["matcher"] for g in pre] == ["run_command", "edit_file|write_file", "Weird"]
    assert "description" not in pre[0]["hooks"][0]
    assert pre[0]["hooks"][0]["command"] == f"python {out.resolve().as_posix()}/hooks/scripts/run.py"
    assert not any("UserPromptSubmit" in v for v in data.values())
    assert {("matcher", "hooks/hooks.json"), ("hook-event", "hooks/hooks.json")} <= _unmapped(report)


def test_hooks_json_is_keyed_by_hook_name_with_count_matching_source_events(plugin, tmp_path):
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    hooks["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "s"}]}]
    hooks["hooks"]["PostToolUse"] = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "p"}]}]
    (plugin / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    data = json.loads((out / "hooks.json").read_text(encoding="utf-8"))
    supported = set(hooks["hooks"]) & set(gen.load_table()["agy"]["hook_events"])
    assert "hooks" not in data
    assert len(data) == len(supported) == 3
    for name, body in data.items():
        assert isinstance(body, dict) and len(body) == 1, name
        (event,) = body
        assert event in supported and isinstance(body[event], list) and body[event], name


def test_claude_only_tool_names_are_rewritten_in_matchers(plugin, tmp_path):
    claude_only = {"Read", "Grep", "Glob", "MultiEdit", "NotebookEdit"}
    hooks = {"hooks": {"PreToolUse": [
        {"matcher": "Read|Grep|Glob", "hooks": [{"type": "command", "command": "a"}]},
        {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "b"}]},
        {"matcher": "Write|Edit|MultiEdit|Bash|PowerShell", "hooks": [{"type": "command", "command": "c"}]},
    ]}}
    (plugin / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    pre = json.loads((out / "hooks.json").read_text(encoding="utf-8"))["pre-tool-use"]["PreToolUse"]
    assert [g["matcher"] for g in pre] == [
        "view_file|grep_search|find_by_name",
        "write_file|edit_file|multi_replace_file_content|notebook_edit",
        "write_file|edit_file|multi_replace_file_content|run_command|PowerShell"]
    for g in pre:
        assert not claude_only & set(g["matcher"].split("|"))
    assert any(u["kind"] == "matcher" and "'PowerShell'" in u["reason"] for u in report["unmapped"])
    assert not any(u["kind"] == "matcher" and any(f"'{t}'" in u["reason"] for t in claude_only) for u in report["unmapped"])


def test_non_tool_events_are_flat_command_handlers_and_tool_events_stay_grouped(plugin, tmp_path):
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    hooks["hooks"]["SessionStart"] = [{"matcher": "startup", "hooks": [
        {"type": "command", "command": "s1"}, {"type": "command", "command": "s2"}]}]
    hooks["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "stop"}]}]
    (plugin / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    named = json.loads((out / "hooks.json").read_text(encoding="utf-8"))
    data = {event: entries for body in named.values() for event, entries in body.items()}
    assert data["SessionStart"] == [{"type": "command", "command": "s1"}, {"type": "command", "command": "s2"}]
    assert data["Stop"] == [{"type": "command", "command": "stop"}]
    assert all("hooks" in g for g in data["PreToolUse"])
    for event, entries in data.items():
        for e in entries:
            for h in e["hooks"] if event in ("PreToolUse", "PostToolUse") else [e]:
                assert h.get("command"), (event, h)
    assert any(u["kind"] == "matcher" and "SessionStart" in u["reason"] for u in report["unmapped"])


def test_manifest_written_at_root_from_source_manifest(plugin, tmp_path):
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "coord", "version": "4.6.17", "description": "D", "logo": "assets/logo.png", "author": "x"}), encoding="utf-8")
    (plugin / "assets").mkdir()
    (plugin / "assets" / "logo.png").write_bytes(b"\x89PNG\x00")
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    assert json.loads((out / "plugin.json").read_text(encoding="utf-8")) == {
        "name": "coord", "version": "4.6.17", "description": "D", "logo": "assets/logo.png"}
    assert (out / "assets" / "logo.png").read_bytes() == b"\x89PNG\x00"
    assert ("surface", ".claude-plugin") not in _unmapped(report)


def test_manifest_without_source_has_no_logo_and_reports(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    manifest = json.loads((out / "plugin.json").read_text(encoding="utf-8"))
    assert manifest == {"name": "plugin"}
    assert ("manifest", ".claude-plugin/plugin.json") in _unmapped(report)


def test_agy_plugin_validate_accepts_generated_tree(plugin, tmp_path):
    agy = shutil.which("agy")
    if not agy:
        pytest.skip("agy not on PATH (validate proves the tree loads, not that a hook fires: "
                    "${PLUGIN_ROOT} expansion and a guard running need a live agy session, covered by nothing here)")
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "coord", "version": "1.0.0", "description": "D", "logo": "assets/logo.png"}),
        encoding="utf-8")
    (plugin / "assets").mkdir()
    (plugin / "assets" / "logo.png").write_bytes(b"\x89PNG\x00")
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    hooks["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "s"}]}]
    hooks["hooks"]["PreToolUse"] = [
        {"matcher": "Read|Grep|Glob", "hooks": [{"type": "command", "command": "a"}]},
        {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": "b"}]}]
    (plugin / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    proc = subprocess.run([agy, "plugin", "validate", str(out), "--strict", "--json"],
                          capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r'"hooks"\s*:\s*2\b', proc.stdout), proc.stdout
    assert not re.search(r'"warnings"\s*:\s*(\[\s*[^\]\s]|[1-9])', proc.stdout), proc.stdout
    assert not re.search(r"never match", proc.stdout), proc.stdout
    assert (out / "rules" / "coordinator-doctrine.md").is_file()
    assert re.search(r'"rules"\s*:\s*1\b', proc.stdout) and "no valid trigger" not in proc.stdout, proc.stdout
    assert re.search(r'"findings"\s*:\s*\[\s*\]', proc.stdout), proc.stdout


def test_commands_become_skills_and_skill_folder_matches_frontmatter_name(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    t = _tree(out)
    assert not (out / "commands").exists()
    assert t["skills/foo/SKILL.md"].startswith(b"---\nname: foo\ndescription: Foo\n---\n")
    assert t["skills/sub-bar/SKILL.md"].startswith(b'---\nname: sub-bar\ndescription: "no frontmatter, no trailing newline"\n---\n')
    assert "skills/real-name/SKILL.md" in t and t["skills/real-name/ref.txt"] == b"ref\n"
    assert "skills/real-dir/SKILL.md" not in t
    assert ("skill", "skills/flat.md") in _unmapped(report)


def test_rules_written_at_table_path_and_unknown_surface_reported(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    rules = gen.load_table()["agy"]["rules_path"]
    assert (out / rules).read_bytes() == b"---\ntrigger: always_on\n---\n# doctrine\n\n- rule\n"
    assert ("surface", "extras") in _unmapped(report)
    assert json.loads((out / "UNMAPPED.json").read_text(encoding="utf-8"))["unmapped"] == report["unmapped"]


def test_oversize_skill_is_reported_and_not_written(plugin, tmp_path):
    (plugin / "skills" / "big").mkdir()
    (plugin / "skills" / "big" / "SKILL.md").write_bytes(b"---\nname: big\n---\n" + b"x" * 24001)
    report = gen.generate(plugin, tmp_path / "out", "agy")
    assert not (tmp_path / "out" / "skills" / "big").exists()
    assert ("oversize", "skills/big/SKILL.md") in _unmapped(report)


@pytest.mark.parametrize("host", ["codex", "grok"])
def test_empty_host_maps_report_every_item_unmappable(plugin, tmp_path, host):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, host)
    assert report["written"] == ["UNMAPPED.json"]
    kinds = {u["kind"] for u in report["unmapped"]}
    assert {"agent", "command", "skill", "hooks", "rules"} <= kinds
    assert {u["source"] for u in report["unmapped"] if u["kind"] == "agent"} == {
        f"agents/{n}.md" for n in ("a-opus", "a-sonnet", "a-haiku", "a-odd")}


def test_unknown_host_raises(plugin, tmp_path):
    with pytest.raises(ValueError):
        gen.generate(plugin, tmp_path / "out", "nope")


def test_every_text_file_is_lf_and_order_is_sorted(plugin, tmp_path):
    (plugin / "hooks" / "scripts" / "blob.bin").write_bytes(b"\x00\r\n\xff")
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    on_disk = _tree(out)
    assert (out / "hooks" / "scripts" / "blob.bin").read_bytes() == b"\x00\r\n\xff"
    for rel, data in on_disk.items():
        if b"\x00" in data:
            continue
        assert b"\r" not in data, rel
        assert data.endswith(b"\n"), rel
    assert report["written"] == sorted(report["written"])
    assert report["written"] == sorted(on_disk)
    assert report["unmapped"] == sorted(report["unmapped"], key=lambda u: (u["kind"], u["source"], u["reason"]))
    raw = (out / "UNMAPPED.json").read_text(encoding="utf-8")
    assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True) + "\n"


def test_symlinks_in_source_are_not_followed_or_created(plugin, tmp_path):
    try:
        (plugin / "agents" / "link.md").symlink_to(plugin / "agents" / "a-opus.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    assert not any(p.is_symlink() for p in out.rglob("*"))
    assert "agents/link.md" not in _tree(out)


def test_out_dir_policy_clears_prior_run_and_refuses_foreign_dirs(plugin, tmp_path):
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    (out / "stale.txt").write_text("old", encoding="utf-8")
    gen.generate(plugin, out, "agy")
    assert not (out / "stale.txt").exists()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ValueError):
        gen.generate(plugin, foreign, "agy")
    assert (foreign / "keep.txt").read_text(encoding="utf-8") == "mine"
    with pytest.raises(ValueError):
        gen.generate(plugin, plugin / "inside", "agy")


def test_generator_imports_only_stdlib_and_no_subprocess():
    tree = ast.parse((_BIN / "unimatrix-plugin-gen.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= set(sys.stdlib_module_names)
    assert "subprocess" not in mods


def test_twice_byte_identical(plugin, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        buf = io.StringIO()
        rc = ui.main(["--hosts", "agy", "--out", str(d), "--plugin-root", str(plugin)], out=buf)
        assert rc == 0, buf.getvalue()
        assert "agy plugin tree written" in buf.getvalue() and "unmapped" in buf.getvalue()
    # hooks.json bakes its own dest dir; normalise it so the two trees compare on content.
    ta = {k: v.replace(a.resolve().as_posix().encode(), b"<DEST>") for k, v in _tree(a).items()}
    tb = {k: v.replace(b.resolve().as_posix().encode(), b"<DEST>") for k, v in _tree(b).items()}
    assert list(ta) == list(tb) and list(ta) == sorted(ta)
    assert ta == tb
    assert "hooks.json" in ta and "UNMAPPED.json" in ta
    assert any(k.startswith("skills/") and k.endswith("/SKILL.md") for k in ta)


def test_out_without_install_seeds_nothing_and_generator_failure_is_partial(plugin, tmp_path):
    hooks = tmp_path / "agy-hooks.json"
    buf = io.StringIO()
    assert ui.main(["--hosts", "agy", "--out", str(tmp_path / "t"), "--plugin-root", str(plugin)],
                   out=buf, hooks_path=hooks) == 0
    assert not hooks.exists()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("mine", encoding="utf-8")
    buf = io.StringIO()
    rc = ui.main(["--hosts", "agy", "--out", str(foreign), "--plugin-root", str(plugin)], out=buf)
    assert rc == 7 and "WARN: agy plugin tree not written" in buf.getvalue()
    assert "not installed: agy plugin tree" in buf.getvalue()
    assert (foreign / "keep.txt").exists()


_DOOR = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/hook-run"'
_PASS2 = "; rc=$?; [ $rc -eq 2 ] && rc=0; exit $rc"


def _door_hooks(plugin, commands):
    groups = [{"matcher": "Bash", "hooks": [{"type": "command", "shell": "bash", "timeout": 5, "command": c}]}
              for c in commands]
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": groups}}), encoding="utf-8")


def _load_shim(tree):
    ns = {"__name__": "agy_hook_run_shim"}
    exec(compile((tree / "hooks" / "agy-hook-run.py").read_text(encoding="utf-8"), "agy-hook-run.py", "exec"), ns)
    return ns["main"], ns


def test_agy_door_hook_commands_are_shell_neutral_and_keep_semantics(plugin, tmp_path):
    cmds = [
        f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.preuse_bash_dispatch",
        f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} --advisory hooks.x_check 2>/dev/null{_PASS2}",
        f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.guard_turn_intent 2>/dev/null{_PASS2}",
    ]
    _door_hooks(plugin, cmds)
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    data = json.loads((out / "hooks.json").read_text(encoding="utf-8"))
    got = [h["command"] for g in data["pre-tool-use"]["PreToolUse"] for h in g["hooks"]]
    path = (out.resolve() / "hooks" / "agy-hook-run.py").as_posix()
    shim = f'{gen._agy_python()} ' + (f'"{path}"' if " " in path else path)
    assert got == [
        f"{shim} hooks.preuse_bash_dispatch",
        f"{shim} --quiet --pass-2 --advisory hooks.x_check",
        f"{shim} --quiet --pass-2 hooks.guard_turn_intent",
    ]
    for c in got:
        assert not re.search(r"^\s*\w+=|rc=\$\?|/dev/null|\$\{\w+:-|;|&&", c.replace("${PLUGIN_ROOT}", ""))
    assert all("shell" not in h for g in data["pre-tool-use"]["PreToolUse"] for h in g["hooks"])
    assert "${PLUGIN_ROOT}" not in (out / "hooks.json").read_text(encoding="utf-8")
    assert (out / "hooks" / "agy-hook-run.py").is_file()
    assert "hook-command" not in {u["kind"] for u in report["unmapped"]}


def test_unrecognised_bash_only_hook_command_is_reported_not_silently_shipped(plugin, tmp_path):
    _door_hooks(plugin, ["FOO=1 some-tool 2>/dev/null"])
    report = gen.generate(plugin, tmp_path / "out", "agy")
    assert "hook-command" in {u["kind"] for u in report["unmapped"]}
    assert not (tmp_path / "out" / "hooks" / "agy-hook-run.py").exists()


def test_shim_sets_stdin_mode_resolves_settings_home_and_maps_exit(plugin, tmp_path, monkeypatch):
    _door_hooks(plugin, [f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.a"])
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    main, ns = _load_shim(out)
    calls = []

    class _R:
        def __init__(self, rc):
            self.returncode = rc

    rcs = iter([2, 2, 0, 5])

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _R(next(rcs))

    monkeypatch.setattr(ns["subprocess"], "run", fake_run)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("COORDINATOR_DOOR_STDIN_MODE", raising=False)
    assert main(["--pass-2", "--advisory", "hooks.a"]) == 0
    assert main(["hooks.a"]) == 2
    assert main(["--quiet", "hooks.a"]) == 0
    assert main(["hooks.a"]) == 5
    cmd, kw = calls[0]
    assert Path(cmd[0]).parent == tmp_path / "home" / "bin" and Path(cmd[0]).stem == "hook-run"
    assert cmd[1:] == ["--advisory", "hooks.a"]
    assert kw["env"]["COORDINATOR_DOOR_STDIN_MODE"] == "hook"
    assert kw["stderr"] is None and calls[2][1]["stderr"] == ns["subprocess"].DEVNULL
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME")
    monkeypatch.setattr(ns["Path"], "home", classmethod(lambda c: tmp_path / "h"))
    rcs = iter([0])
    main(["hooks.a"])
    assert Path(calls[-1][0][0]).parent == tmp_path / "h" / ".coordinator-claude-settings" / "bin"


@pytest.mark.skipif(sys.platform == "win32", reason="door stand-in is a POSIX script; the mocked leg covers Windows")
def test_shim_runs_end_to_end_with_a_real_child_process(plugin, tmp_path):
    _door_hooks(plugin, [f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.a"])
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    exe = home / "bin" / "hook-run"
    exe.write_text('#!/bin/sh\ncat; echo "$COORDINATOR_DOOR_STDIN_MODE $*"; exit 2\n', encoding="utf-8")
    exe.chmod(0o755)
    r = subprocess.run([sys.executable, str(out / "hooks" / "agy-hook-run.py"), "--pass-2", "hooks.a"],
                       input="EVT", capture_output=True, text=True, env={"COORDINATOR_SETTINGS_HOME": str(home)})
    assert r.returncode == 0 and r.stdout == "EVThook hooks.a\n"


@pytest.mark.parametrize("os_name,exe", [("nt", "python"), ("posix", "python3")])
def test_agy_launcher_interpreter_is_the_one_the_os_resolves(monkeypatch, os_name, exe, tmp_path):
    monkeypatch.setattr(gen.os, "name", os_name)
    cmd = gen._agy_hook_command(f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.a", tmp_path)
    assert cmd.startswith(exe + " ")
