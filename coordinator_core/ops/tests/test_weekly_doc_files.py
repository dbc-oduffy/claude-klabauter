"""Tests for weekly_doc_files: view path over a tmp sqlite db, git fallback over a tmp repo."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import weekly_doc_files as wdf


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "docs/plans").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "docs/plans/p1.md").write_text(
        "x" * 15000 + "\n```yaml\nwrites: [a/b.py, docs/wiki/w.md]\n```\n", encoding="utf-8"
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "add plan")
    _git(r, "rm", "-q", "docs/plans/p1.md")
    _git(r, "commit", "-qm", "complete p1")
    return r


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
        INSERT INTO _pd VALUES ('x.py','docs/wiki/x.md');
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
        "x.py": ["docs/wiki/x.md"],
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


def test_git_fallback_completed_and_deleted_preview(repo: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    rows = wdf.plans_completed("2000-01-01", "2030-01-01", index_path=missing, repo=repo)
    assert len(rows) == 1
    row = rows[0]
    assert row["plan"] == "docs/plans/p1.md" and row["summary"] == "complete p1"
    assert row["sizing"] is None
    out = wdf.plan_preview(row["plan"], row["sha"], index_path=missing, repo=repo)
    assert 0 < len(out) <= wdf.PREVIEW_CAP
    head_out = wdf.plan_preview(row["plan"], None, index_path=missing, repo=repo)
    assert 0 < len(head_out) <= wdf.PREVIEW_CAP


def test_git_fallback_footprint_and_docs(repo: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    fp = wdf.plan_footprint("docs/plans/p1.md", index_path=missing, repo=repo)
    assert fp == ["a/b.py", "docs/wiki/w.md"]
    assert wdf.paths_to_docs(["docs/wiki/w.md", "a/b.py"], index_path=missing) == {
        "docs/wiki/w.md": ["docs/wiki/w.md"],
        "a/b.py": [],
    }


def test_git_fallback_previews_batch_one_spawn(repo: Path, tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing.sqlite"
    sha = wdf.plans_completed("2000-01-01", "2030-01-01", index_path=missing, repo=repo)[0]["sha"]
    calls: list = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    out = wdf.plan_previews(
        [("docs/plans/p1.md", sha), ("docs/plans/p1.md", None), ("docs/plans/nope.md", sha)],
        index_path=missing,
        repo=repo,
    )
    assert len(calls) == 1
    assert len(out[0]) == wdf.PREVIEW_CAP and out[1] == "" and out[2] == ""
