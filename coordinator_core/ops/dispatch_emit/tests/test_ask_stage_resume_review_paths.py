"""A resume's review paths include the rows it dropped as landed, without staging or overlap-checking them."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import PLAN_REL, RUN, _plan, _row


def _capture_overlap(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(ask_stage, "_refuse_live_write_overlap", lambda root, run_id, paths: seen.extend(paths))
    return seen


def test_resume_review_paths_include_the_landed_row_it_dropped(tmp_path, monkeypatch):
    _plan(tmp_path, _row("C1") + _row("C2"))
    overlap = _capture_overlap(monkeypatch)
    m = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=["C2"])
    assert [r.id for r in m.rows] == ["C2"]
    assert "docs/x/C1.md" in m.review_declared_paths
    assert "docs/x/C2.md" in m.review_declared_paths
    assert m.review_declared_paths.index("docs/x/C2.md") < m.review_declared_paths.index("docs/x/C1.md")
    assert "docs/x/C1.md" not in overlap
    assert "docs/x/C1.md" not in {w for r in m.rows for w in r.writes}


def test_fresh_run_review_paths_are_just_its_rows(tmp_path, monkeypatch):
    _plan(tmp_path, _row("C1") + _row("C2"))
    _capture_overlap(monkeypatch)
    m = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL)
    assert set(m.review_declared_paths) == {"docs/x/C1.md", "docs/x/C2.md"}


def _code_row(rid: str) -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: code-edit\n  surface: pkg/{rid}.py\n"
        f"  writes:\n    - pkg/{rid}.py\n"
    )


def test_ledger_resume_review_code_paths_include_landed_code_not_declared(tmp_path, monkeypatch):
    _plan(tmp_path, _code_row("C1") + _row("C2"))
    _capture_overlap(monkeypatch)
    m = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=["C2"], ledger_resume=True)
    assert "pkg/C1.py" in m.review_code_paths
    assert "pkg/C1.py" not in m.review_declared_paths
    assert len(m.review_code_paths) == len(set(m.review_code_paths))


def test_non_resume_only_rows_code_paths_unchanged_by_resume_branch(tmp_path, monkeypatch):
    _plan(tmp_path, _code_row("C1") + _row("C2"))
    _capture_overlap(monkeypatch)
    m = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=["C2"])
    assert "pkg/C1.py" in m.review_declared_paths
    assert list(m.review_code_paths).count("pkg/C1.py") == 1


def test_ledger_resume_review_paths_exclude_dropped_rows(tmp_path, monkeypatch):
    _plan(tmp_path, _row("C1") + _row("C2"))
    _capture_overlap(monkeypatch)
    m = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, only_rows=["C2"], ledger_resume=True)
    assert "docs/x/C1.md" not in m.review_declared_paths
    assert "docs/x/C2.md" in m.review_declared_paths
