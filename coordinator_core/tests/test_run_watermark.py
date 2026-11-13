"""Tests for coordinator_core.run_watermark: rag path (zero spawns), git fallback (one
spawn), select/cursor advance, and the stage/promote commit gate."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordinator_core import run_watermark as rw


@pytest.fixture
def spawns(monkeypatch):
    calls: list[list[str]] = []
    real = rw.git_out

    def counting(argv, **kw):
        calls.append(list(argv))
        return real(argv, **kw)

    monkeypatch.setattr(rw, "git_out", counting)
    return calls


def _db(path: Path, rows, generation=7) -> str:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE doc_files (path TEXT, content_hash TEXT, indexed_commit TEXT,"
        " index_generation INTEGER, change_seen_at TEXT)"
    )
    conn.execute("CREATE TABLE generation (index_generation INTEGER)")
    conn.executemany("INSERT INTO doc_files VALUES (?, 'h', ?, ?, 't')", rows)
    conn.execute("INSERT INTO generation VALUES (?)", (generation,))
    conn.commit()
    conn.close()
    return str(path)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_rag_path_reads_generation_without_spawn(tmp_path, monkeypatch, spawns):
    db = _db(
        tmp_path / "rag.db",
        [("docs/a.md", "c1", 3), ("docs/b.md", "c1", 9), ("other/c.md", "c1", 9)],
    )
    monkeypatch.setattr(rw, "registry_get", lambda key: db)
    wm = rw.Watermark("distill", "c1", base_generation=5)
    changed = rw.changed_since(tmp_path, wm, scope_dirs=["docs"])
    assert set(changed) == {"docs/b.md"}
    assert changed.generation == 7
    assert spawns == []


def test_rag_path_commit_compare_when_no_generation(tmp_path, monkeypatch, spawns):
    db = _db(tmp_path / "rag.db", [("docs/a.md", "old", 1), ("docs/b.md", "new", 1)])
    monkeypatch.setattr(rw, "registry_get", lambda key: db)
    changed = rw.changed_since(tmp_path, rw.Watermark("distill", "new"), scope_dirs=["docs"])
    assert set(changed) == {"docs/a.md"}
    assert spawns == []


def test_rag_path_commit_compare_counts_null_commit_rows(tmp_path, monkeypatch, spawns):
    db = _db(tmp_path / "rag.db", [("docs/a.md", None, 1), ("docs/b.md", "new", 1)])
    monkeypatch.setattr(rw, "registry_get", lambda key: db)
    changed = rw.changed_since(tmp_path, rw.Watermark("distill", "new"), scope_dirs=["docs"])
    assert set(changed) == {"docs/a.md"}


def test_first_run_is_empty_and_spawns_nothing(tmp_path, monkeypatch, spawns):
    monkeypatch.setattr(rw, "registry_get", lambda key: None)
    changed = rw.changed_since(tmp_path, None, scope_dirs=["docs"])
    assert set(changed) == set() and spawns == []


def test_git_fallback_one_spawn_counts_uncommitted_edit(tmp_path, monkeypatch, spawns):
    monkeypatch.setattr(rw, "registry_get", lambda key: None)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("1")
    (tmp_path / "docs" / "b.md").write_text("1")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "docs" / "a.md").write_text("2")
    changed = rw.changed_since(
        tmp_path, rw.Watermark("distill", base), scope_dirs=["docs"]
    )
    assert set(changed) == {"docs/a.md"}
    assert len(spawns) == 1


def test_git_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "registry_get", lambda key: None)
    with pytest.raises(rw.ChangedSinceError):
        rw.changed_since(tmp_path, rw.Watermark("distill", "deadbeef"), scope_dirs=["docs"])


def test_select_first_run_pins_base_and_advances_cursor():
    cands = ["a", "b", "c", "d"]
    changed = rw.ChangedSet()
    changed.generation = 4
    scoped, proposed = rw.select(cands, None, changed, 2, kind="distill", head_sha="H1")
    assert scoped == {"a", "b"}
    assert proposed == rw.Watermark("distill", "H1", 4, "b", False)


def test_select_mid_pass_keeps_base_and_adds_changed():
    wm = rw.Watermark("distill", "H1", 4, "b", False)
    changed = rw.ChangedSet({"a", "zzz"})
    scoped, proposed = rw.select(["a", "b", "c", "d", "e"], wm, changed, 2, head_sha="H2")
    assert scoped == {"a", "c", "d"}
    assert proposed.base_sha == "H1" and proposed.cursor == "d" and not proposed.pass_complete


def test_select_pass_completion_moves_base_to_head():
    wm = rw.Watermark("distill", "H1", 4, "c", False)
    changed = rw.ChangedSet()
    changed.generation = 9
    scoped, proposed = rw.select(["a", "b", "c", "d"], wm, changed, 5, head_sha="H2")
    assert scoped == {"d"}
    assert proposed == rw.Watermark("distill", "H2", 9, "d", True)


def test_select_completed_pass_is_pure_delta():
    wm = rw.Watermark("distill", "H1", 4, "d", True)
    changed = rw.ChangedSet({"b"})
    scoped, proposed = rw.select(["a", "b", "c", "d"], wm, changed, 5, head_sha="H2")
    assert scoped == {"b"}
    assert proposed.base_sha == "H2" and proposed.cursor == "d" and proposed.pass_complete


def test_stage_then_promote_writes_state_only_on_promote(tmp_path):
    repo, run_dir = tmp_path / "repo", tmp_path / "run"
    proposed = rw.Watermark("distill", "H1", None, "b", False)
    rw.stage(run_dir, proposed)
    assert (run_dir / rw.PENDING_NAME).is_file()
    assert rw.load(repo, "distill") is None
    path = rw.promote(repo, run_dir, "distill")
    assert path == repo / "state" / "run-watermarks" / "distill.json"
    assert rw.load(repo, "distill") == proposed


def test_promote_without_pending_writes_nothing(tmp_path):
    repo = tmp_path / "repo"
    assert rw.promote(repo, tmp_path / "run", "distill") is None
    assert not (repo / "state").exists()


def test_load_rejects_malformed(tmp_path):
    p = rw.watermark_path(tmp_path, "distill")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert rw.load(tmp_path, "distill") is None
