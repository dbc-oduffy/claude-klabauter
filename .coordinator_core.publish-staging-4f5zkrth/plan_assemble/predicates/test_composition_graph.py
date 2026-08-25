"""Tests for coordinator_core.plan_assemble.predicates.composition_graph (C6).

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C6

Every fixture plan lives under a per-test `tmp_path` — never the live
`docs/plans/` tree, per the plan's Test surface note (AC10).
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates.composition_graph import (
    amends_assumption,
    chunk_overlap,
    cross_plan_conflict,
    path_rename_or_move,
)
from coordinator_core.win_portability import no_console_creationflags

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_SPINE_HEADER = "# fixture plan\n\n## Tasks\n\n"
_NO_CONSOLE = no_console_creationflags()


def _ctx(repo_root, plan_path=None, plan_frontmatter=None, plan_body=None) -> PredicateContext:
    return PredicateContext(
        repo_root=repo_root,
        plan_path=plan_path,
        plan_frontmatter=plan_frontmatter,
        plan_body=plan_body,
        sizing_object_path=None,
        sizing_frontmatter=None,
        resolved_route="clean",
        caller_flags={},
    )


def _write_spine_plan(path, spine_body: str, frontmatter: str = "") -> None:
    text = ""
    if frontmatter:
        text += f"---\n{frontmatter}\n---\n"
    text += _SPINE_HEADER + "```yaml plan-tasks\n" + spine_body + "\n```\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# chunk_overlap (:151)
# ---------------------------------------------------------------------------


def test_chunk_overlap_no_plan_undetermined(tmp_path):
    result = chunk_overlap(_ctx(tmp_path))
    assert result["undetermined"] is True
    assert "no --plan" in result["reason"]


def test_chunk_overlap_unreadable_spine_undetermined(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# no task-spine block here\n", encoding="utf-8")
    result = chunk_overlap(_ctx(tmp_path, plan_path=plan_path))
    assert result["undetermined"] is True


def test_chunk_overlap_reports_pairwise_intersection(tmp_path):
    spine = """\
- id: C1
  title: writes a shared dir
  surface: some/surface
  writes:
    - some/shared/
- id: C2
  title: writes a file under that dir
  surface: some/surface
  writes:
    - some/shared/file.py
- id: C3
  title: disjoint writer
  surface: some/surface
  writes:
    - other/place.py
"""
    plan_path = tmp_path / "plan.md"
    _write_spine_plan(plan_path, spine)
    result = chunk_overlap(_ctx(tmp_path, plan_path=plan_path))

    assert result["pairs"] == [
        {
            "chunk_a": "C1",
            "chunk_b": "C2",
            "overlapping_paths": ["some/shared/", "some/shared/file.py"],
        }
    ]


def test_chunk_overlap_skips_undeclared_writes(tmp_path):
    spine = """\
- id: C1
  title: undeclared writes
  surface: some/surface
- id: C2
  title: declared writes
  surface: some/surface
  writes:
    - some/file.py
"""
    plan_path = tmp_path / "plan.md"
    _write_spine_plan(plan_path, spine)
    result = chunk_overlap(_ctx(tmp_path, plan_path=plan_path))
    assert result["pairs"] == []


# ---------------------------------------------------------------------------
# path_rename_or_move (:156)
# ---------------------------------------------------------------------------


def test_path_rename_or_move_no_plan_undetermined(tmp_path):
    result = path_rename_or_move(_ctx(tmp_path))
    assert result["undetermined"] is True


def test_path_rename_or_move_no_cited_paths(tmp_path):
    spine = """\
- id: C1
  title: no writes or reads
  surface: some/surface
"""
    plan_path = tmp_path / "plan.md"
    _write_spine_plan(plan_path, spine)
    result = path_rename_or_move(_ctx(tmp_path, plan_path=plan_path))
    assert result == {"fires": False, "paths": []}


def test_path_rename_or_move_detects_a_real_rename(tmp_path):
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, **_NO_CONSOLE)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, **_NO_CONSOLE
    )
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True, **_NO_CONSOLE)

    old_file = repo / "old_name.py"
    old_file.write_text("x = 1\n" * 5, encoding="utf-8")
    subprocess.run(["git", "add", "old_name.py"], cwd=repo, check=True, **_NO_CONSOLE)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add old_name.py"], cwd=repo, check=True, **_NO_CONSOLE
    )

    new_file = repo / "new_name.py"
    subprocess.run(["git", "mv", "old_name.py", "new_name.py"], cwd=repo, check=True, **_NO_CONSOLE)
    subprocess.run(
        ["git", "commit", "-q", "-m", "rename to new_name.py"], cwd=repo, check=True, **_NO_CONSOLE
    )

    spine = """\
- id: C1
  title: cites the renamed file
  surface: some/surface
  writes:
    - new_name.py
"""
    plan_path = repo / "plan.md"
    _write_spine_plan(plan_path, spine)
    result = path_rename_or_move(_ctx(repo, plan_path=plan_path))
    assert result["fires"] is True
    assert result["paths"] == ["new_name.py"]


def test_path_rename_or_move_git_timeout_is_undetermined(tmp_path, monkeypatch):
    spine = """\
- id: C1
  title: cites a path
  surface: some/surface
  writes:
    - some/file.py
"""
    plan_path = tmp_path / "plan.md"
    _write_spine_plan(plan_path, spine)

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "log"], timeout=15)

    monkeypatch.setattr(
        "coordinator_core.plan_assemble.predicates.composition_graph._run_git",
        _raise_timeout,
    )
    result = path_rename_or_move(_ctx(tmp_path, plan_path=plan_path))
    assert result["undetermined"] is True
    assert "git log --follow failed" in result["reason"]


def test_path_rename_or_move_git_oserror_is_undetermined(tmp_path, monkeypatch):
    spine = """\
- id: C1
  title: cites a path
  surface: some/surface
  writes:
    - some/file.py
"""
    plan_path = tmp_path / "plan.md"
    _write_spine_plan(plan_path, spine)

    def _raise_oserror(*args, **kwargs):
        raise OSError("git executable not found")

    monkeypatch.setattr(
        "coordinator_core.plan_assemble.predicates.composition_graph._run_git",
        _raise_oserror,
    )
    result = path_rename_or_move(_ctx(tmp_path, plan_path=plan_path))
    assert result["undetermined"] is True
    assert "git log --follow failed" in result["reason"]


# ---------------------------------------------------------------------------
# cross_plan_conflict (:160)
# ---------------------------------------------------------------------------


def test_cross_plan_conflict_no_plan_undetermined(tmp_path):
    result = cross_plan_conflict(_ctx(tmp_path))
    assert result["undetermined"] is True


def test_cross_plan_conflict_no_own_scope_returns_empty(tmp_path):
    result = cross_plan_conflict(_ctx(tmp_path, plan_frontmatter={}))
    assert result == {"hits": []}


def test_cross_plan_conflict_finds_scope_overlap(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    this_plan = plans_dir / "this-plan.md"
    this_plan.write_text("---\nscope:\n  - some/shared/\n---\nbody\n", encoding="utf-8")

    sibling = plans_dir / "sibling-plan.md"
    sibling.write_text(
        "---\nscope:\n  - some/shared/file.py\n---\nsibling body\n", encoding="utf-8"
    )

    disjoint = plans_dir / "disjoint-plan.md"
    disjoint.write_text("---\nscope:\n  - other/place.py\n---\nbody\n", encoding="utf-8")

    ctx = _ctx(
        tmp_path,
        plan_path=this_plan,
        plan_frontmatter={"scope": ["some/shared/"]},
    )
    result = cross_plan_conflict(ctx)

    assert result["hits"] == [
        {
            "plan_path": "docs/plans/sibling-plan.md",
            "overlapping_paths": ["some/shared/", "some/shared/file.py"],
        }
    ]


def test_cross_plan_conflict_skips_closed_sibling(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    this_plan = plans_dir / "this-plan.md"
    this_plan.write_text("---\nscope:\n  - some/shared/\n---\nbody\n", encoding="utf-8")

    closed_sibling = plans_dir / "closed-sibling-plan.md"
    closed_sibling.write_text(
        "---\nstatus: closed\nscope:\n  - some/shared/file.py\n---\nbody\n", encoding="utf-8"
    )

    ctx = _ctx(
        tmp_path,
        plan_path=this_plan,
        plan_frontmatter={"scope": ["some/shared/"]},
    )
    result = cross_plan_conflict(ctx)

    assert result["hits"] == []


# ---------------------------------------------------------------------------
# amends_assumption (:162)
# ---------------------------------------------------------------------------


def test_amends_assumption_no_plan_undetermined(tmp_path):
    result = amends_assumption(_ctx(tmp_path))
    assert result["undetermined"] is True


def test_amends_assumption_no_table_undetermined(tmp_path):
    plan_path = tmp_path / "plan.md"
    result = amends_assumption(
        _ctx(tmp_path, plan_path=plan_path, plan_body="# just prose, no table\n")
    )
    assert result["undetermined"] is True
    assert "Cross-plan coordination" in result["reason"]


def test_amends_assumption_finds_a_text_match(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    this_plan = plans_dir / "this-plan.md"
    body = (
        "## Cross-plan coordination\n\n"
        "| Sibling plan | Assumption it carries | Disposition |\n"
        "|---|---|---|\n"
        "| `sibling-plan.md` | Wave 1 always returns gates as an empty dict | Extended |\n"
    )
    this_plan.write_text(body, encoding="utf-8")

    sibling = plans_dir / "sibling-plan.md"
    sibling.write_text(
        "---\nstatus: open\n---\n"
        "Wave 1 always returns gates as an empty dict, by construction.\n",
        encoding="utf-8",
    )

    closed_sibling = plans_dir / "closed-plan.md"
    closed_sibling.write_text(
        "---\nstatus: closed\n---\n"
        "Wave 1 always returns gates as an empty dict, by construction.\n",
        encoding="utf-8",
    )

    result = amends_assumption(_ctx(tmp_path, plan_path=this_plan, plan_body=body))
    assert result["candidate"] is True
    assert result["matched_plan"] == "docs/plans/sibling-plan.md"


def test_amends_assumption_no_match_returns_false(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    this_plan = plans_dir / "this-plan.md"
    body = (
        "## Cross-plan coordination\n\n"
        "| Sibling plan | Assumption it carries | Disposition |\n"
        "|---|---|---|\n"
        "| `sibling-plan.md` | A wildly specific and unmatched assumption string | Extended |\n"
    )
    this_plan.write_text(body, encoding="utf-8")

    sibling = plans_dir / "sibling-plan.md"
    sibling.write_text(
        "---\nstatus: open\n---\nSomething entirely unrelated.\n", encoding="utf-8"
    )

    result = amends_assumption(_ctx(tmp_path, plan_path=this_plan, plan_body=body))
    assert result == {"candidate": False, "matched_plan": None}
