"""Behavior pins for unimatrix-plugin-gen.py (table-driven plugin tree translation) and its installer wiring."""
import ast
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

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
    assert b"${PLUGIN_DIR}/x" in t["agents/a-opus.md"] and b"CLAUDE_PLUGIN_ROOT" not in t["agents/a-opus.md"]


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
    pre = data["hooks"]["PreToolUse"]
    assert [g["matcher"] for g in pre] == ["run_command", "edit_file|write_file", "Weird"]
    assert "description" not in pre[0]["hooks"][0]
    assert pre[0]["hooks"][0]["command"] == "python ${PLUGIN_DIR}/hooks/scripts/run.py"
    assert "UserPromptSubmit" not in data["hooks"]
    assert {("matcher", "hooks/hooks.json"), ("hook-event", "hooks/hooks.json")} <= _unmapped(report)


def test_commands_become_skills_and_skill_folder_matches_frontmatter_name(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    t = _tree(out)
    assert not (out / "commands").exists()
    assert t["skills/foo/SKILL.md"].startswith(b"---\nname: foo\ndescription: Foo\n---\n")
    assert t["skills/sub-bar/SKILL.md"].startswith(b"---\nname: sub-bar\n---\n")
    assert "skills/real-name/SKILL.md" in t and t["skills/real-name/ref.txt"] == b"ref\n"
    assert "skills/real-dir/SKILL.md" not in t
    assert ("skill", "skills/flat.md") in _unmapped(report)


def test_rules_written_at_table_path_and_unknown_surface_reported(plugin, tmp_path):
    out = tmp_path / "out"
    report = gen.generate(plugin, out, "agy")
    rules = gen.load_table()["agy"]["rules_path"]
    assert (out / rules).read_bytes() == b"# doctrine\n\n- rule\n"
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
    ta, tb = _tree(a), _tree(b)
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
