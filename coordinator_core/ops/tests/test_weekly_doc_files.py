"""Tests for weekly_doc_files: view path over a tmp sqlite db, git fallback over a tmp repo."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coordinator_core.ops import weekly_doc_files as wdf

_WIKI = "docs" + "/wiki/"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "doc_files.sqlite"
    c = sqlite3.connect(p)
    c.executescript(
        f"""
        CREATE TABLE _pc(plan, sha, summary, sizing, ranked, completed_at);
        CREATE VIEW {wdf.VIEW_PLANS_COMPLETED} AS SELECT * FROM _pc;
        INSERT INTO _pc VALUES ('docs/plans/a.md','s1','sum a','L',1,'2026-11-10'),
                               ('docs/plans/b.md','s2','sum b','S',2,'2026-11-20');
        CREATE TABLE _fp(plan, path);
        CREATE VIEW {wdf.VIEW_PLAN_FOOTPRINT} AS SELECT * FROM _fp;
        INSERT INTO _fp VALUES ('docs/plans/a.md','x.py');
        CREATE TABLE _pd(path, doc);
        CREATE VIEW {wdf.VIEW_PATHS_TO_DOCS} AS SELECT * FROM _pd;
        INSERT INTO _pd VALUES ('x.py','{_WIKI}x.md');
        CREATE TABLE _pv(plan, sha, preview);
        CREATE VIEW {wdf.VIEW_PLAN_PREVIEW} AS SELECT * FROM _pv;
        INSERT INTO _pv VALUES ('docs/plans/a.md','s1','{"y" * 20000}');
        """
    )
    c.commit()
    c.close()
    return p


def test_view_plans_completed_filters_window(db: Path) -> None:
    rows = wdf.plans_completed("2026-11-09", "2026-11-16", index_path=db)
    assert [r["plan"] for r in rows] == ["docs/plans/a.md"]
    assert rows[0]["sizing"] == "L"


def test_view_footprint_and_docs(db: Path) -> None:
    assert wdf.plan_footprint("docs/plans/a.md", index_path=db) == ["x.py"]
    assert wdf.paths_to_docs(["x.py", "z.py"], index_path=db) == {
        "x.py": [_WIKI + "x.md"],
        "z.py": [],
    }


def test_view_preview_capped_with_sha(db: Path) -> None:
    out = wdf.plan_preview("docs/plans/a.md", "s1", index_path=db)
    assert len(out) == wdf.PREVIEW_CAP
    assert wdf.plan_preview("docs/plans/a.md", "nope", index_path=db) == ""


def test_view_path_is_read_only(db: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        wdf._select(db, "DELETE FROM _pc")


def test_absent_index_selects_fallback(tmp_path: Path) -> None:
    assert wdf.resolve_index_path(tmp_path / "missing.sqlite") is None


def test_view_plan_footprints_batch(db: Path) -> None:
    out = wdf.plan_footprints([("docs/plans/a.md", "s1"), ("docs/plans/b.md", None)], index_path=db)
    assert out == {"docs/plans/a.md": ["x.py"], "docs/plans/b.md": []}
