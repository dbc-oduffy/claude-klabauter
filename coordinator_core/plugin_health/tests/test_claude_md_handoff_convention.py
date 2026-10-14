"""Tests for the fleet CLAUDE.md handoff-convention lint."""
from __future__ import annotations

from coordinator_core.plugin_health import claude_md_handoff_convention as mod


def _repo(tmp_path, name, text, rel="CLAUDE.md"):
    root = tmp_path / name
    target = root / rel
    target.parent.mkdir(parents=True)
    target.write_text(text, encoding="utf-8")
    return root


def test_retired_path_is_flagged(tmp_path):
    root = _repo(tmp_path, "a", "Write notes into .claude/handoffs/today.md\n")
    out = mod.report({"a": root})
    assert len(out) == 1
    assert out[0].startswith("a: CLAUDE.md:1:")
    assert mod.REMEDY in out[0]


def test_negated_mention_is_not_flagged(tmp_path):
    root = _repo(tmp_path, "a", "The retired .claude/handoffs path is gone.\nNever write a handoff to disk by hand.\n")
    assert mod.report({"a": root}) == []


def test_manual_write_phrase_is_flagged_in_nested_claude_md(tmp_path):
    root = _repo(tmp_path, "a", "Always write a handoff file manually.\n", rel=".claude/CLAUDE.md")
    out = mod.report({"a": root})
    assert len(out) == 1 and ".claude/CLAUDE.md:1:" in out[0]


def test_unreadable_repo_is_skipped(tmp_path):
    assert mod.report({"gone": tmp_path / "missing"}) == []


def test_fleet_repos_reads_registry_dedupes_and_skips_missing(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    raw = {
        "repos.one": str(root),
        "repos.alias": str(root),
        "repos.missing": str(tmp_path / "nope"),
        "other.key": str(root),
        "repos.notstr": 3,
    }
    assert mod.fleet_repos(raw) == {"one": root}


def test_main_always_returns_zero_and_prints_offences(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, "a", "save handoff to .claude/handoffs\n")
    monkeypatch.setattr(mod, "fleet_repos", lambda raw=None: {"a": root})
    assert mod.main([]) == 0
    assert "a: CLAUDE.md:1:" in capsys.readouterr().out


def test_main_skips_on_registry_failure(monkeypatch, capsys):
    def boom(raw=None):
        raise RuntimeError("no registry")

    monkeypatch.setattr(mod, "fleet_repos", boom)
    assert mod.main([]) == 0
    assert "lint skipped -- no registry" in capsys.readouterr().out
