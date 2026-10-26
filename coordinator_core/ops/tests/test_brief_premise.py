"""Tests for the read-only EM-brief premise check."""

import subprocess

from coordinator_core.ops import _brief_premise as bp


def _tree(tmp_path):
    f = tmp_path / "coordinator_core" / "ops" / "real_door.py"
    f.parent.mkdir(parents=True)
    f.write_text("def real_fn():\n    return 1\n", encoding="utf-8")
    return tmp_path


def _by(rows, claim):
    return next(r for r in rows if r["claim"] == claim)


def test_r40_shaped_wrong_door_is_contradicted(tmp_path):
    root = _tree(tmp_path)
    brief = "Fix it in coordinator_core/ops/wrong_door.py, the door is `real_fn` in coordinator_core/ops/real_door.py."
    rows = bp.check_brief_claims(brief, root)
    assert _by(rows, "coordinator_core/ops/wrong_door.py")["verdict"] == "CONTRADICTED"
    assert _by(rows, "coordinator_core/ops/real_door.py")["verdict"] == "RESOLVES"
    assert _by(rows, "real_fn in coordinator_core/ops/real_door.py")["verdict"] == "RESOLVES"
    out = bp.render_contradicted(rows)
    assert out.startswith("CONTRADICTED: coordinator_core/ops/wrong_door.py")
    assert "real_door" not in out


def test_missing_symbol_and_backslash_path(tmp_path):
    root = _tree(tmp_path)
    rows = bp.check_brief_claims(r"see `ghost` in coordinator_core\ops\real_door.py", root)
    assert _by(rows, "ghost in coordinator_core\\ops\\real_door.py")["verdict"] == "CONTRADICTED"
    assert _by(rows, "coordinator_core/ops/real_door.py")["verdict"] == "RESOLVES"


def test_hex_ref_uncheckable_and_plain_words_ignored(tmp_path):
    rows = bp.check_brief_claims("lever-register.md @3749abc and the word deadbeefs plus a/b", tmp_path)
    assert _by(rows, "3749abc")["verdict"] == "UNCHECKABLE"
    assert _by(rows, "3749abc")["kind"] == "ref"
    assert all(r["verdict"] in bp.VERDICTS for r in rows)


def test_no_contradictions_renders_empty(tmp_path):
    root = _tree(tmp_path)
    rows = bp.check_brief_claims("coordinator_core/ops/real_door.py", root)
    assert bp.render_contradicted(rows) == ""
    assert bp.check_brief_claims("", root) == []


def test_urls_are_not_path_claims(tmp_path):
    assert bp.check_brief_claims("https://example.com/a/b.md", tmp_path) == []


def test_still_returns_rows_when_subprocess_spawn_fails(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("spawn denied")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    rows = bp.check_brief_claims("x/y.py @3749abc", _tree(tmp_path))
    assert {r["verdict"] for r in rows} == {"CONTRADICTED", "UNCHECKABLE"}
