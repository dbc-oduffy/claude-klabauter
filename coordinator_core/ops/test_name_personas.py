"""Characterization + parity tests for coordinator_core.ops.name_personas.

Port source: coordinator/dist/publish-repo-setup/name-personas.sh (DoE-claude, 291 lines).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.name_personas import (
    KNOWN_ROLES,
    count_prose_matches,
    main,
    replace_in_prose,
    to_slug,
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    agents = repo / "plugins" / "coordinator" / "agents"
    agents.mkdir(parents=True)
    (agents / "staff-eng.md").write_text(
        "---\n"
        "name: staff-eng\n"
        "---\n"
        "the Staff Engineer reviews code. The Staff Engineer is exacting.\n"
        "a staff engineer with exacting standards is not the same sentinel.\n"
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "customization.md").write_text("Meet the Game Dev Reviewer.\n")
    return repo


# ---------------------------------------------------------------------------
# to_slug — Unicode-normalize + strip combining marks + lowercase
# ---------------------------------------------------------------------------

def test_to_slug_ascii():
    assert to_slug("Alex") == "alex"


def test_to_slug_strips_combining_diacritics():
    assert to_slug("José") == "jose"


# ---------------------------------------------------------------------------
# frontmatter-safe helpers
# ---------------------------------------------------------------------------

def test_count_prose_matches_skips_frontmatter():
    text = "---\nname: the Staff Engineer\n---\nthe Staff Engineer reviews code.\n"
    assert count_prose_matches("the Staff Engineer", text) == 1


def test_replace_in_prose_preserves_frontmatter():
    text = "---\nname: the Staff Engineer\n---\nthe Staff Engineer reviews code.\n"
    out = replace_in_prose("the Staff Engineer", "Alex", text)
    assert "name: the Staff Engineer" in out
    assert "Alex reviews code." in out


def test_replace_in_prose_no_frontmatter_full_body():
    text = "the Staff Engineer reviews code.\n"
    out = replace_in_prose("the Staff Engineer", "Alex", text)
    assert out == "Alex reviews code.\n"


# ---------------------------------------------------------------------------
# main() — argv validation
# ---------------------------------------------------------------------------

def test_main_odd_arg_count_errors(capsys):
    rc = main(["the Staff Engineer"])
    assert rc == 1
    assert "must be ROLE NAME pairs" in capsys.readouterr().err


def test_main_zero_args_errors(capsys):
    rc = main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no ROLE NAME pairs provided" in err
    for role in KNOWN_ROLES:
        assert role in err


def test_main_unknown_role_errors(capsys):
    rc = main(["the Intern", "Sam"])
    assert rc == 1
    assert "is not a known role label" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() — dry-run mode (no mutation)
# ---------------------------------------------------------------------------

def test_main_dry_run_reports_no_mutation(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("NAME_PERSONAS_REPO_ROOT", str(repo))
    target = repo / "plugins" / "coordinator" / "agents" / "staff-eng.md"
    original = target.read_text()

    rc = main(["--dry-run", "the Staff Engineer", "Alex"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Dry-run mode" in out
    assert "the Staff Engineer → Alex: 1 replacement(s)" in out
    assert "The Staff Engineer → Alex: 1 replacement(s)" in out
    assert target.read_text() == original
    assert not (repo / "plugins" / "coordinator" / "agents" / "staff-eng.md.bak").exists()


# ---------------------------------------------------------------------------
# main() — live run
# ---------------------------------------------------------------------------

def test_main_live_run_substitutes_articulated_forms_only(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("NAME_PERSONAS_REPO_ROOT", str(repo))
    target = repo / "plugins" / "coordinator" / "agents" / "staff-eng.md"

    rc = main(["the Staff Engineer", "Alex", "the Game Dev Reviewer", "Jordan"])

    assert rc == 0
    text = target.read_text()
    assert "Alex reviews code. Alex is exacting." in text
    assert "a staff engineer with exacting standards is not the same sentinel." in text
    assert "name: staff-eng" in text  # frontmatter untouched

    cust = repo / "docs" / "customization.md"
    assert cust.read_text() == "Meet Jordan.\n"

    out = capsys.readouterr().out
    assert "Persona names bound:" in out
    assert "Total files modified: 2" in out


def test_main_live_run_leaves_bak_backup(tmp_path, monkeypatch):
    """`.bak` is overwritten per matching substitution pair (oracle `perl -i.bak`
    fidelity) — with two matching pairs ("the X" then sentence-initial "The X"),
    the final `.bak` snapshot reflects state after the FIRST pair but before the
    SECOND, not the pristine original. Single-file-single-pair fixtures below
    assert the true-original case; this one pins the two-pair carry-forward."""
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("NAME_PERSONAS_REPO_ROOT", str(repo))
    target = repo / "plugins" / "coordinator" / "agents" / "staff-eng.md"

    rc = main(["the Staff Engineer", "Alex"])

    assert rc == 0
    bak = target.parent / (target.name + ".bak")
    assert bak.is_file()
    # after "the Staff Engineer" pair applied, before "The Staff Engineer" pair
    assert "Alex reviews code. The Staff Engineer is exacting." in bak.read_text()


def test_main_live_run_bak_is_pristine_for_single_matching_pair(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("NAME_PERSONAS_REPO_ROOT", str(repo))
    cust = repo / "docs" / "customization.md"
    original = cust.read_text()

    rc = main(["the Game Dev Reviewer", "Jordan"])

    assert rc == 0
    bak = cust.parent / (cust.name + ".bak")
    assert bak.is_file()
    assert bak.read_text() == original


def test_main_collision_audit_notes_unarticulated_form(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("NAME_PERSONAS_REPO_ROOT", str(repo))

    rc = main(["the Staff Engineer", "Alex"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "'Staff Engineer' (unarticulated form of 'the Staff Engineer')" in out
    assert "will NOT be substituted" in out
