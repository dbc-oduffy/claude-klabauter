"""Behavior tests for coordinator_core.ops.verify_skill_anchor_links.

Covers path-directed resolution (each citation checked against the file IT
names), the optional doctrine-surface manifest, and the 0/1/2 exit-code split
between "checked, clean", "checked, dead anchors" and "could not check".

Spec backlink: coordinator/commands/update-docs.md § Phase 11h
Origin: verify-skill-anchor-links.sh (coordinator-claude b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.verify_skill_anchor_links import _plugin_root, main, scan


DOCTRINE_A = """## How to Plan and Hand Off

### Fan-out is the default dispatch shape

## How to Decide
"""

DOCTRINE_B = """## Exit Routing

## Only In File B
"""

GLOBAL_DOCTRINE = """## Engineering Defaults

## Operating Assumptions
"""


def _make_tree(tmp_path: Path, skill_md_body: str) -> Path:
    """Build a repo-root/plugin-root pair mirroring coordinator-claude's real layout.

    repo_root/
      coordinator/                 <- plugin_root
        snippets/doctrine-a.md
        skills/plan/SKILL.md
      global-doctrine/CLAUDE.md
    """
    repo_root = tmp_path / "repo"
    plugin_root = repo_root / "coordinator"
    (plugin_root / "snippets").mkdir(parents=True)
    (plugin_root / "skills" / "plan").mkdir(parents=True)
    (repo_root / "global-doctrine").mkdir(parents=True)

    (plugin_root / "snippets" / "doctrine-a.md").write_text(DOCTRINE_A)
    (plugin_root / "snippets" / "doctrine-b.md").write_text(DOCTRINE_B)
    (repo_root / "global-doctrine" / "CLAUDE.md").write_text(GLOBAL_DOCTRINE)
    (plugin_root / "skills" / "plan" / "SKILL.md").write_text(skill_md_body)
    return plugin_root


def _consumers(plugin_root: Path):
    return [str(plugin_root / "skills" / "plan" / "SKILL.md")]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DOCTRINE_MANIFEST", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


def _write_manifest(tmp_path: Path, monkeypatch, payload) -> Path:
    path = tmp_path / "doctrine-surfaces.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    monkeypatch.setenv("COORDINATOR_DOCTRINE_MANIFEST", str(path))
    return path


# --- path-directed resolution -------------------------------------------------

def test_ok_match_exact_heading(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n",
    )
    report = scan(str(root), _consumers(root))
    assert report.error == ""
    assert not report.skipped
    assert [r.kind for r in report.results] == ["OK"]
    assert report.results[0].value == "How to Decide"


def test_dead_when_heading_absent_from_cited_file(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § Nonexistent Heading._\n",
    )
    report = scan(str(root), _consumers(root))
    assert report.error == ""
    assert [r.kind for r in report.results] == ["DEAD"]


def test_ok_and_dead_against_different_cited_files_in_one_consumer(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n"
        "_See `coordinator/snippets/doctrine-b.md` § How to Decide._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [(r.kind, r.line_no) for r in report.results] == [("OK", 1), ("DEAD", 2)]


def test_heading_present_only_in_another_file_is_DEAD_not_unioned(tmp_path):
    """Anti-union regression guard — the single most important property here.

    `Only In File B` exists as a heading in doctrine-b.md and nowhere else. A
    citation naming doctrine-a.md must be DEAD. If a future edit unions the
    surface set's headings into one list, this citation goes green while
    pointing at a section that does not exist in the file it names — partial
    success reading as health.
    """
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § Only In File B._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["DEAD"]


def test_longest_prefix_match_prefers_subsection(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § Fan-out is the default "
        "dispatch shape._\n",
    )
    report = scan(str(root), _consumers(root))
    assert report.results[0].kind == "OK"
    assert report.results[0].value == "Fan-out is the default dispatch shape"


def test_path_resolves_relative_to_plugin_root(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `snippets/doctrine-a.md` § How to Decide._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["OK"]


def test_path_resolves_relative_to_the_citing_files_own_directory(tmp_path):
    root = _make_tree(tmp_path, "_See `sibling.md` § Local Heading._\n")
    (root / "skills" / "plan" / "sibling.md").write_text("## Local Heading\n")
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["OK"]


def test_second_section_marker_on_a_line_carries_the_cited_path_forward(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § How to Decide and "
        "§ How to Plan and Hand Off._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["OK", "OK"]
    assert {r.cited_path for r in report.results} == {"coordinator/snippets/doctrine-a.md"}


def test_double_marker_citation_is_scanned(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` §§ How to Decide / How to "
        "Plan and Hand Off._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["OK"]


def test_formerly_annotation_is_historical_not_dead(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § How to Decide (formerly "
        "§ Push Back, Ask, Don't-Ask, and Close)._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["OK"]
    assert report.historical == 1


def test_carried_citation_missing_on_a_global_line_is_qualified_not_dead(tmp_path):
    """coordinator-claude prose puts the file AFTER the section as often as before it."""
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § How to Decide and "
        "§ Operating Assumptions (global `~/.claude/CLAUDE.md`)._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["OK", "QUALIFIED"]


def test_path_naming_citation_missing_on_a_global_line_is_still_dead(tmp_path):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § No Such Heading "
        "(global `~/.claude/CLAUDE.md` also applies)._\n",
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["DEAD"]


def test_line_without_a_md_citation_is_ignored(tmp_path):
    root = _make_tree(tmp_path, "This line has a § but no .md path in front of it.\n")
    report = scan(str(root), _consumers(root))
    assert report.error == ""
    assert report.results == []


def test_bare_section_marker_with_drifted_punctuation_is_dropped_but_counted(tmp_path):
    """Finding 6 (P2) — a `§` whose preceding punctuation falls outside the

    tight citation regex (e.g. a colon between path and `§`) is silently
    never scanned as a citation, but must be visible as a coarse
    format-drift diagnostic distinct from a line with no `§` at all.
    """
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md`: § How to Decide._\n",
    )
    report = scan(str(root), _consumers(root))
    assert report.results == []
    assert report.dropped_section_lines == 1


def test_missing_consumer_is_skipped_not_errored(tmp_path):
    root = _make_tree(
        tmp_path, "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n"
    )
    consumers = _consumers(root) + [str(root / "skills" / "nonexistent" / "SKILL.md")]
    report = scan(str(root), consumers)
    assert report.error == ""
    assert len(report.skipped) == 1
    assert len(report.results) == 1


def test_all_consumers_missing_is_could_not_check_not_a_clean_zero(tmp_path):
    """Finding 1 (P1) — total wipeout must not degrade to exit 0/zero coverage.

    If every consumer path in the allowlist has vanished, that is COULD NOT
    CHECK (error set, would exit 2 via main()), never a clean 0 with
    total=0/dead=0 — the exact defect class the surrounding exit-code
    rewrite exists to close.
    """
    root = _make_tree(
        tmp_path, "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n"
    )
    consumers = [
        str(root / "skills" / "gone-a" / "SKILL.md"),
        str(root / "skills" / "gone-b" / "SKILL.md"),
    ]
    report = scan(str(root), consumers)
    assert report.error != ""
    assert "consumer" in report.error.lower()
    assert len(report.skipped) == 2
    assert report.results == []


# --- UNRESOLVED ---------------------------------------------------------------

def test_unresolved_path_is_reported_but_not_fatal(tmp_path, monkeypatch, capsys):
    root = _make_tree(tmp_path, "_See `OVERVIEW.md` § Some Cluster._\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "UNRESOLVED" in out
    assert "unresolved=1" in out
    assert "dead=0" in out


# --- qualification (no manifest) ---------------------------------------------

def test_qualified_global_tilde_path_when_no_manifest(tmp_path):
    root = _make_tree(tmp_path, "_See ~/.claude/CLAUDE.md § Anything At All._\n")
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["QUALIFIED"]


def test_qualified_global_word_marker_when_path_does_not_resolve(tmp_path):
    root = _make_tree(
        tmp_path, "_See the global doctrine.md § Operating Assumptions (global)._\n"
    )
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["QUALIFIED"]


def test_manifest_absent_is_not_an_error_and_notes_it(tmp_path, monkeypatch, capsys):
    root = _make_tree(
        tmp_path,
        "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n"
        "_See ~/.claude/CLAUDE.md § Anything At All._\n",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "ok=1" in captured.out
    assert "qualified=1" in captured.out
    assert "no doctrine-surface manifest" in captured.err


def test_manifest_absent_still_exits_1_on_a_dead_anchor(tmp_path, monkeypatch, capsys):
    root = _make_tree(
        tmp_path, "_See `coordinator/snippets/doctrine-a.md` § Nonexistent._\n"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 1
    assert "dead=1" in capsys.readouterr().out


# --- manifest present and valid ----------------------------------------------

def _valid_manifest_payload():
    return {
        "schema_version": 1,
        "surfaces": [
            "coordinator/snippets/doctrine-a.md",
            "global-doctrine/CLAUDE.md",
        ],
        "aliases": {"~/.claude/CLAUDE.md": "global-doctrine/CLAUDE.md"},
    }


def test_valid_manifest_makes_an_alias_citation_checkable_OK(tmp_path, monkeypatch):
    root = _make_tree(tmp_path, "_See ~/.claude/CLAUDE.md § Engineering Defaults._\n")
    _write_manifest(tmp_path, monkeypatch, _valid_manifest_payload())
    report = scan(str(root), _consumers(root))
    assert report.error == ""
    assert [r.kind for r in report.results] == ["OK"]
    assert report.results[0].value == "Engineering Defaults"


def test_valid_manifest_makes_an_alias_citation_checkable_DEAD(tmp_path, monkeypatch):
    root = _make_tree(tmp_path, "_See ~/.claude/CLAUDE.md § No Such Heading._\n")
    _write_manifest(tmp_path, monkeypatch, _valid_manifest_payload())
    report = scan(str(root), _consumers(root))
    assert [r.kind for r in report.results] == ["DEAD"]


def test_valid_manifest_emits_no_absent_note(tmp_path, monkeypatch):
    root = _make_tree(tmp_path, "_See ~/.claude/CLAUDE.md § Engineering Defaults._\n")
    _write_manifest(tmp_path, monkeypatch, _valid_manifest_payload())
    report = scan(str(root), _consumers(root))
    assert report.notes == []


# --- manifest present and broken ⇒ exit 2 ------------------------------------

def test_unparseable_manifest_exits_2(tmp_path, monkeypatch, capsys):
    root = _make_tree(tmp_path, "_See `coordinator/snippets/doctrine-a.md` § X._\n")
    _write_manifest(tmp_path, monkeypatch, "{not json at all")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 2
    assert "unparseable JSON" in capsys.readouterr().err


def test_wrong_schema_version_manifest_exits_2(tmp_path, monkeypatch, capsys):
    root = _make_tree(tmp_path, "_See `coordinator/snippets/doctrine-a.md` § X._\n")
    payload = _valid_manifest_payload()
    payload["schema_version"] = 2
    _write_manifest(tmp_path, monkeypatch, payload)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 2
    assert "unknown schema_version" in capsys.readouterr().err


def test_manifest_missing_schema_version_exits_2(tmp_path, monkeypatch, capsys):
    root = _make_tree(tmp_path, "_See `coordinator/snippets/doctrine-a.md` § X._\n")
    payload = _valid_manifest_payload()
    del payload["schema_version"]
    _write_manifest(tmp_path, monkeypatch, payload)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 2
    assert "unknown schema_version" in capsys.readouterr().err


def test_manifest_surfaces_entry_missing_from_disk_exits_2(tmp_path, monkeypatch, capsys):
    root = _make_tree(tmp_path, "_See `coordinator/snippets/doctrine-a.md` § X._\n")
    payload = _valid_manifest_payload()
    payload["surfaces"].append("coordinator/snippets/vanished.md")
    _write_manifest(tmp_path, monkeypatch, payload)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "missing from disk" in err
    assert "vanished.md" in err


def test_manifest_alias_target_missing_from_disk_exits_2_with_alias_wording(
    tmp_path, monkeypatch, capsys
):
    """Finding 4 (nit) — alias-target failure must name the alias, not say

    'surfaces entry' for a target that came from `aliases`, not `surfaces`.
    """
    root = _make_tree(tmp_path, "_See `coordinator/snippets/doctrine-a.md` § X._\n")
    payload = _valid_manifest_payload()
    payload["aliases"]["~/.claude/CLAUDE.md"] = "global-doctrine/vanished-alias-target.md"
    _write_manifest(tmp_path, monkeypatch, payload)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "missing from disk" in err
    assert "~/.claude/CLAUDE.md" in err
    assert "vanished-alias-target.md" in err
    assert "surfaces entry" not in err


def test_manifest_malformed_shape_exits_2(tmp_path, monkeypatch, capsys):
    root = _make_tree(tmp_path, "_See `coordinator/snippets/doctrine-a.md` § X._\n")
    _write_manifest(tmp_path, monkeypatch, {"schema_version": 1, "surfaces": "not-a-list"})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 2
    assert "malformed" in capsys.readouterr().err


def test_missing_plugin_root_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "nowhere"))
    assert main([]) == 2
    assert "plugin root not found" in capsys.readouterr().err


# --- exit-code split ----------------------------------------------------------

def test_main_exit_code_0_on_clean(tmp_path, monkeypatch, capsys):
    root = _make_tree(
        tmp_path, "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 0
    assert "dead=0" in capsys.readouterr().out


def test_main_exit_code_1_on_dead(tmp_path, monkeypatch, capsys):
    root = _make_tree(
        tmp_path, "_See `coordinator/snippets/doctrine-a.md` § Nonexistent Heading._\n"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main([]) == 1
    out = capsys.readouterr().out
    assert "DEAD" in out
    assert "dead=1" in out


# --- --list mode --------------------------------------------------------------

def test_main_list_mode(tmp_path, monkeypatch, capsys):
    root = _make_tree(
        tmp_path, "_See `coordinator/snippets/doctrine-a.md` § How to Decide._\n"
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "skills/plan/SKILL.md" in out
    assert "skills/review/SKILL.md" in out
    assert "skills/review-code/SKILL.md" in out


def test_main_list_mode_does_not_require_any_doctrine_file(tmp_path, monkeypatch, capsys):
    root = tmp_path / "bare-plugin-root"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert main(["--list"]) == 0
    assert "skills/plan/SKILL.md" in capsys.readouterr().out


# --- _plugin_root resolution --------------------------------------------------

def test_plugin_root_claude_plugin_root_env_wins_verbatim(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/some/explicit/plugin/root")
    assert _plugin_root() == "/some/explicit/plugin/root"


def test_plugin_root_unset_resolves_via_doe_root(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "/scratch/coordinator-claude")
    assert _plugin_root() == os.path.join("/scratch/coordinator-claude", "coordinator")


def test_plugin_root_unset_and_unresolvable_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("REPO_EXAMPLE_DOCTRINE_REPO", "")
    monkeypatch.setattr(
        "coordinator_core.ops.verify_skill_anchor_links.coordinator_doe_root",
        lambda: None,
    )
    with pytest.raises(SystemExit) as exc_info:
        _plugin_root()
    assert exc_info.value.code == 2
    assert "cannot resolve the coordinator root" in capsys.readouterr().err
