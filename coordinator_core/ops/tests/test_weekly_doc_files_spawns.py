"""Tests for weekly_doc_files: view path over a tmp sqlite db, git fallback over a tmp repo."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import weekly_doc_files as wdf

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WIKI = "docs" + "/wiki/"


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
        "x" * 15000 + "\n```yaml\nwrites: [a/b.py, " + _WIKI + "w.md]\n```\n", encoding="utf-8"
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
        INSERT INTO _pd VALUES ('x.py','{_WIKI}x.md');
        CREATE TABLE _pv(plan, sha, preview);
        CREATE VIEW {wdf.VIEW_PLAN_PREVIEW} AS SELECT * FROM _pv;
        INSERT INTO _pv VALUES ('docs/plans/a.md','s1','{"y" * 20000}');
        """
    )
    c.commit()
    c.close()
    return p


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
    assert fp == ["a/b.py", _WIKI + "w.md"]
    assert wdf.paths_to_docs([_WIKI + "w.md", "a/b.py"], index_path=missing, repo=repo) == {
        _WIKI + "w.md": [_WIKI + "w.md"],
        "a/b.py": [],
    }


def _wiki(repo: Path, name: str, text: str) -> None:
    (repo / "docs/wiki").mkdir(parents=True, exist_ok=True)
    (repo / "docs/wiki" / name).write_text(text, encoding="utf-8")


def test_fallback_paths_to_docs_matches_wiki_text(repo: Path, tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    _wiki(repo, "one.md", "See `a/b.py` and `core/x.py::fn`.\n")
    _wiki(repo, "two.md", "Only mentions a/b.pyc and other/a/b.py.\n")
    _wiki(repo, "three.md", "Names a/b.py, " + _WIKI + "w.md.\n")
    out = wdf.paths_to_docs(
        ["a/b.py", "core/x.py", _WIKI + "w.md", "nope.py"], index_path=missing, repo=repo
    )
    assert out["a/b.py"] == [_WIKI + "one.md", _WIKI + "three.md"]
    assert out["core/x.py"] == [_WIKI + "one.md"]
    assert out[_WIKI + "w.md"] == [_WIKI + "w.md", _WIKI + "three.md"]
    assert out["nope.py"] == []


def test_fallback_wiki_cache_rereads_only_changed_pages(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    missing = tmp_path / "missing.sqlite"
    _wiki(repo, "one.md", "a/b.py\n")
    _wiki(repo, "two.md", "c/d.py\n")
    reads: list = []
    real = wdf._read_page_tokens
    monkeypatch.setattr(wdf, "_read_page_tokens", lambda p: reads.append(p) or real(p))
    wdf.paths_to_docs(["a/b.py"], index_path=missing, repo=repo)
    assert len(reads) == 2
    reads.clear()
    assert wdf.paths_to_docs(["c/d.py"], index_path=missing, repo=repo) == {
        "c/d.py": [_WIKI + "two.md"]
    }
    assert reads == []
    _wiki(repo, "two.md", "c/d.py e/f.py\n")
    assert wdf.paths_to_docs(["e/f.py"], index_path=missing, repo=repo) == {
        "e/f.py": [_WIKI + "two.md"]
    }
    assert [Path(p).name for p in reads] == ["two.md"]


def test_view_paths_to_docs_ignores_wiki_text(db: Path, repo: Path) -> None:
    _wiki(repo, "one.md", "z.py\n")
    assert wdf.paths_to_docs(["z.py"], index_path=db, repo=repo) == {"z.py": []}


def test_fallback_plan_footprints_one_spawn_matches_per_plan(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    missing = tmp_path / "missing.sqlite"
    sha = wdf.plans_completed("2000-01-01", "2030-01-01", index_path=missing, repo=repo)[0]["sha"]
    calls: list = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    out = wdf.plan_footprints(
        [("docs/plans/p1.md", sha), ("docs/plans/nope.md", sha)], index_path=missing, repo=repo
    )
    assert len(calls) == 1
    assert out == {"docs/plans/p1.md": ["a/b.py", _WIKI + "w.md"], "docs/plans/nope.md": []}
    assert out["docs/plans/p1.md"] == wdf.plan_footprint(
        "docs/plans/p1.md", index_path=missing, repo=repo
    )


def test_fallback_plan_footprints_uses_supplied_texts(repo: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    out = wdf.plan_footprints(
        [("p.md", None)], index_path=tmp_path / "missing.sqlite", repo=repo,
        texts=["writes: [q/r.py]\n"],
    )
    assert out == {"p.md": ["q/r.py"]}


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
