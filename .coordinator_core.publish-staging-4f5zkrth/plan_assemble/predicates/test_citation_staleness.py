"""
coordinator_core.plan_assemble.predicates.test_citation_staleness — Leg 1
(scope-path staleness) and Leg 2 (content-anchored cited-line staleness)
for contract row `:85-87`.

Includes the moved-but-intact case (AC8) and the genuinely-changed case as
distinct assertions — a suite that cannot tell those apart has not tested
this row.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C11
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates import citation_staleness as cs

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _context(tmp_path: Path, **overrides) -> PredicateContext:
    defaults = dict(
        repo_root=tmp_path,
        plan_path=None,
        plan_frontmatter=None,
        plan_body=None,
        sizing_object_path=None,
        sizing_frontmatter=None,
        resolved_route="plan",
        caller_flags={},
    )
    defaults.update(overrides)
    return PredicateContext(**defaults)


# --- Leg 1: scope_paths_staleness ---------------------------------------


def test_scope_paths_undetermined_when_no_plan_frontmatter(tmp_path):
    result = cs.scope_paths_staleness(_context(tmp_path))
    assert result["undetermined"] is True
    assert result["reason"]


def test_scope_paths_empty_scope_is_clean(tmp_path):
    result = cs.scope_paths_staleness(_context(tmp_path, plan_frontmatter={}))
    assert result == {"scope_paths_stale": False, "stale_paths": []}


def test_scope_paths_present_path_is_not_stale(tmp_path):
    (tmp_path / "coordinator_core").mkdir()
    (tmp_path / "coordinator_core" / "foo.py").write_text("x = 1\n")
    result = cs.scope_paths_staleness(
        _context(
            tmp_path,
            plan_frontmatter={"scope": ["coordinator_core/foo.py"]},
        )
    )
    assert result == {"scope_paths_stale": False, "stale_paths": []}


def test_scope_paths_missing_path_with_no_history_is_stale(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    result = cs.scope_paths_staleness(
        _context(
            tmp_path,
            plan_frontmatter={"scope": ["never/existed.py"]},
        )
    )
    assert result["scope_paths_stale"] is True
    assert result["stale_paths"] == [
        {
            "path": "never/existed.py",
            "reason": "absent on disk, no git history found for this path",
        }
    ]


def test_scope_paths_missing_path_with_prior_history_is_stale(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path, check=True, creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, check=True, creationflags=_NO_WINDOW,
    )
    target = tmp_path / "gone.py"
    target.write_text("x = 1\n")
    subprocess.run(["git", "add", "gone.py"], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add gone.py"],
        cwd=tmp_path, check=True, creationflags=_NO_WINDOW,
    )
    subprocess.run(["git", "rm", "-q", "gone.py"], cwd=tmp_path, check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove gone.py"],
        cwd=tmp_path, check=True, creationflags=_NO_WINDOW,
    )

    result = cs.scope_paths_staleness(
        _context(tmp_path, plan_frontmatter={"scope": ["gone.py"]})
    )
    assert result["scope_paths_stale"] is True
    assert result["stale_paths"] == [
        {
            "path": "gone.py",
            "reason": (
                "absent on disk but has prior git history "
                "(deleted or renamed away)"
            ),
        }
    ]


def test_scope_paths_accepts_bare_string_scope(tmp_path):
    result = cs.scope_paths_staleness(
        _context(tmp_path, plan_frontmatter={"scope": "coordinator_core/x.py"})
    )
    assert result["scope_paths_stale"] is True
    assert result["stale_paths"][0]["path"] == "coordinator_core/x.py"


# --- Leg 2: cited_lines_staleness ---------------------------------------


def test_cited_lines_undetermined_when_no_plan_body(tmp_path):
    result = cs.cited_lines_staleness(_context(tmp_path))
    assert result["undetermined"] is True
    assert result["reason"]


def test_cited_lines_no_citations_is_clean(tmp_path):
    result = cs.cited_lines_staleness(
        _context(tmp_path, plan_body="Just some prose with no citations at all.")
    )
    assert result == {"cited_lines_stale": False, "stale_citations": []}


def test_cited_lines_no_anchor_text_is_undetermined_entry(tmp_path):
    target = tmp_path / "foo.py"
    target.write_text("line one\nline two\n")
    body = "See `foo.py:1` for details."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    assert result["cited_lines_stale"] is False
    entry = result["stale_citations"][0]
    assert entry["undetermined"] is True
    assert entry["path"] == "foo.py"
    assert entry["cited_line"] == 1


def test_cited_lines_target_file_absent_is_stale(tmp_path):
    body = "The `some_symbol` lives at `missing.py:5`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    assert result["cited_lines_stale"] is True
    entry = result["stale_citations"][0]
    assert entry["stale"] is True
    assert entry["reason"] == "target file absent"


def test_cited_lines_exact_match_at_cited_line_is_not_stale(tmp_path):
    target = tmp_path / "foo.py"
    target.write_text("a = 1\nb = 2\ndef some_symbol():\n    pass\n")
    body = "The `def some_symbol():` line is at `foo.py:3`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    assert result["cited_lines_stale"] is False
    entry = result["stale_citations"][0]
    assert entry["stale"] is False
    assert "moved_to" not in entry


def test_cited_lines_moved_but_intact_is_not_stale_with_moved_to(tmp_path):
    """AC8: a citation whose target text moved by N lines but is
    textually intact reports `stale: False` with a `moved_to` line, not
    a false positive from naive line-equality."""
    target = tmp_path / "foo.py"
    target.write_text(
        "a = 1\n"
        "b = 2\n"
        "c = 3\n"
        "d = 4\n"
        "def some_symbol():\n"
        "    pass\n"
    )
    # Citation claims the def was at line 3; three lines were inserted
    # above it, so it now actually lives at line 5.
    body = "The `def some_symbol():` line is at `foo.py:3`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    assert result["cited_lines_stale"] is False
    entry = result["stale_citations"][0]
    assert entry["stale"] is False
    assert entry["moved_to"] == 5
    assert entry["cited_line"] == 3


def test_cited_lines_genuinely_changed_is_stale(tmp_path):
    """The DISTINCT case from moved-but-intact: the anchor text is gone
    from the target file entirely (not moved, not paraphrased-and-found
    elsewhere) -> a definite `stale: True`, not `undetermined`."""
    target = tmp_path / "foo.py"
    target.write_text("a = 1\nb = 2\ndef totally_different():\n    pass\n")
    body = "The `def some_symbol():` line is at `foo.py:3`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    assert result["cited_lines_stale"] is True
    entry = result["stale_citations"][0]
    assert entry["stale"] is True
    assert entry["reason"] == (
        "anchor text not found anywhere in target file (genuine content change)"
    )


def test_cited_lines_normalised_whitespace_rung_matches_rewrapped_line(tmp_path):
    target = tmp_path / "foo.py"
    target.write_text("x = 1\ny    =     2\n")
    body = "The assignment `y = 2` sits at `foo.py:2`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    entry = result["stale_citations"][0]
    assert entry["stale"] is False


def test_cited_lines_symbol_rung_matches_bare_identifier_declaration(tmp_path):
    target = tmp_path / "foo.py"
    target.write_text("a = 1\nb = 2\nc = 3\ndef some_symbol(x, y):\n    pass\n")
    body = "`some_symbol` is defined at `foo.py:1`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    entry = result["stale_citations"][0]
    assert entry["stale"] is False
    assert entry["moved_to"] == 4


def test_cited_lines_target_unreadable_is_undetermined(tmp_path):
    target = tmp_path / "binfile.py"
    target.write_bytes(b"\xff\xfe\x00\x01not-utf8\x80")
    body = "See `something` in `binfile.py:1`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    entry = result["stale_citations"][0]
    assert entry.get("undetermined") is True
    assert entry["reason"] == "target file unreadable"


def test_cited_lines_whitespace_only_anchor_span_is_undetermined_not_a_bogus_match(tmp_path):
    """Regression: a stray whitespace-only backtick span adjacent to a
    citation (e.g. `` ` ` ``) must not be treated as real anchor text —
    it would otherwise match nearly every non-empty target line at rung 1
    (`anchor_text in line`), producing a false non-`undetermined` result."""
    target = tmp_path / "foo.py"
    target.write_text("a = 1\nb = 2\n")
    body = "See ` ` next to `foo.py:1` for details."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    entry = result["stale_citations"][0]
    assert entry["undetermined"] is True
    assert entry["reason"] == "no adjacent quoted anchor text found near citation"


def test_cited_lines_multiple_citations_each_get_own_entry(tmp_path):
    (tmp_path / "a.py").write_text("def alpha():\n    pass\n")
    (tmp_path / "b.py").write_text("def beta():\n    pass\n")
    body = "`alpha` is at `a.py:1` and `beta` is at `b.py:1`."
    result = cs.cited_lines_staleness(_context(tmp_path, plan_body=body))
    assert len(result["stale_citations"]) == 2
    assert result["cited_lines_stale"] is False
