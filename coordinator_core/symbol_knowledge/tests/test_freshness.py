"""Tests for coordinator_core.symbol_knowledge.freshness against a synthetic example-retrieval-repo store."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordinator_core.symbol_knowledge import agent_batches, contract, freshness, leg
from coordinator_core.symbol_knowledge.tests.test_flag import (
    ALPHA,
    BOX,
    PARENT_SRC,
    PREFIX,
    SIZE,
    _git,
    _span,
    _symtab,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

OTHER = PREFIX + "Box#other()."
NEW_SRC = PARENT_SRC.replace("return 1", "return 11").replace("return 3", "return 33").replace("return 4", "return 44")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store(root: Path, files: "dict[str, str]", symbols: "list[tuple]", *, files_table: bool = True) -> None:
    (root / ".project-rag").mkdir(exist_ok=True)
    db = sqlite3.connect(root / freshness.STORE_REL)
    if files_table:
        db.execute("CREATE TABLE rc_v1_files (file TEXT, sha256 TEXT, byte_size INTEGER, indexed_at TEXT)")
        db.executemany("INSERT INTO rc_v1_files VALUES (?,?,0,'t')", files.items())
    db.execute(
        "CREATE TABLE rc_v1_symbols (symbol_id INTEGER, name TEXT, kind TEXT, scope TEXT, module TEXT,"
        " signature TEXT, docstring TEXT, file TEXT, line INTEGER, line_end INTEGER, is_definition INTEGER)"
    )
    db.executemany(
        "INSERT INTO rc_v1_symbols (name, kind, scope, docstring, file, is_definition) VALUES (?,?,?,?,?,1)",
        symbols,
    )
    db.commit()
    db.close()


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "pkg" / "mod.py").write_text(PARENT_SRC, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "parent")
    rows = [
        (ALPHA, "function", *_span(PARENT_SRC, "def alpha", "return 1")),
        (BOX, "class", *_span(PARENT_SRC, "class Box", "return 4")),
        (SIZE, "method", *_span(PARENT_SRC, "def size", "return 3")),
        (OTHER, "method", *_span(PARENT_SRC, "def other", "return 4")),
    ]
    graph_db = _symtab(tmp_path / "idx" / "root", rows)
    monkeypatch.setattr(leg, "index_graph_db", lambda _root: graph_db)
    (root / "pkg" / "mod.py").write_text(NEW_SRC, encoding="utf-8", newline="\n")
    (root / "pkg" / "fresh.txt").write_text("indexed already\n", encoding="utf-8")
    (root / "pkg" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "extra.txt").write_text("never indexed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "change")
    sha = _git(root, "rev-parse", "HEAD")
    files = {
        "pkg/mod.py": _sha(NEW_SRC.encode()),
        "pkg/fresh.txt": _sha(b"indexed already\n"),
        "pkg/new.py": _sha(b"old content\n"),
    }
    symbols = [
        ("alpha", "function", "", "Returns one.", "pkg/mod.py"),
        ("Box.size", "method", "", None, "pkg/mod.py"),
    ]
    _store(root, files, symbols)
    return root, sha


def test_freshness_reextracts_only_stale_files_and_queues_only_undocumented(fx, monkeypatch):
    root, sha = fx
    spawned = []
    real = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or real(*a, **k))
    out = freshness.freshness(root, sha)
    assert spawned == []
    assert out["status"] == "ok"
    assert out["files_changed"] == 4
    assert out["stale_files"] == ["pkg/extra.txt", "pkg/new.py"]
    assert out["files_reextracted"] == 2
    assert [q["qualified_name"] for q in out["queued"]] == [SIZE]
    assert out["skipped_documented"] == 1
    assert out["cost"]["spawns"] == 0 and out["cost"]["files_reextracted"] == 2
    assert out["cost"]["process_ms"] < 500


def test_symbol_without_a_store_row_is_unresolved_and_not_queued(fx):
    root, sha = fx
    out = freshness.freshness(root, sha)
    assert out["tier0_unresolved"] == [OTHER]
    assert OTHER not in [q["qualified_name"] for q in out["queued"]]


def test_duplicate_store_rows_leave_the_symbol_unresolved(fx):
    root, sha = fx
    db = sqlite3.connect(root / freshness.STORE_REL)
    db.execute(
        "INSERT INTO rc_v1_symbols (name, kind, scope, docstring, file) VALUES ('alpha','function','',NULL,'pkg/mod.py')"
    )
    db.commit()
    db.close()
    out = freshness.freshness(root, sha)
    assert ALPHA in out["tier0_unresolved"]
    assert ALPHA not in [q["qualified_name"] for q in out["queued"]]


def test_blank_docstring_counts_as_empty_and_scope_form_matches(fx):
    root, sha = fx
    db = sqlite3.connect(root / freshness.STORE_REL)
    db.execute("DELETE FROM rc_v1_symbols WHERE name='Box.size'")
    db.execute(
        "INSERT INTO rc_v1_symbols (name, kind, scope, docstring, file) VALUES ('size','method','Box','  ','pkg/mod.py')"
    )
    db.commit()
    db.close()
    out = freshness.freshness(root, sha)
    assert [q["qualified_name"] for q in out["queued"]] == [SIZE]


def test_unchanged_hash_is_not_reextracted(fx):
    root, sha = fx
    db = sqlite3.connect(root / freshness.STORE_REL)
    db.execute("UPDATE rc_v1_files SET sha256=? WHERE file='pkg/new.py'", (_sha(b"x = 1\n"),))
    db.execute("INSERT INTO rc_v1_files VALUES ('pkg/extra.txt', ?, 0, 't')", (_sha(b"never indexed\n"),))
    db.commit()
    db.close()
    out = freshness.freshness(root, sha)
    assert out["files_changed"] == 4 and out["files_reextracted"] == 0 and out["stale_files"] == []


def test_head_resolves_and_launch_batches_is_never_called(fx, monkeypatch):
    root, _sha_ = fx

    def boom(*_a, **_k):
        raise AssertionError("freshness must not launch a batch")

    monkeypatch.setattr(contract, "launch_batches", boom)
    monkeypatch.setattr(agent_batches, "launch_batches", boom)
    out = contract.freshness(root, "HEAD")
    assert out["status"] == "ok" and len(out["queued"]) == 1


def test_missing_store_is_not_applicable_naming_path_and_table(tmp_path):
    out = freshness.freshness(tmp_path, "HEAD")
    assert out["status"] == "not_applicable"
    assert out["store"] == freshness.STORE_REL and out["table"] == freshness.FILES_TABLE


def test_missing_table_is_not_applicable_naming_the_table(fx):
    root, sha = fx
    (root / freshness.STORE_REL).unlink()
    _store(root, {}, [], files_table=False)
    out = freshness.freshness(root, sha)
    assert out["status"] == "not_applicable" and out["table"] == freshness.FILES_TABLE


def test_tables_served_as_views_are_read(fx):
    root, sha = fx
    db = sqlite3.connect(root / freshness.STORE_REL)
    db.execute("ALTER TABLE rc_v1_files RENAME TO raw_files")
    db.execute("CREATE VIEW rc_v1_files AS SELECT file, sha256, byte_size, indexed_at FROM raw_files")
    db.commit()
    db.close()
    assert freshness.freshness(root, sha)["files_reextracted"] == 2


def test_store_is_opened_read_only(fx):
    root, sha = fx
    before = (root / freshness.STORE_REL).read_bytes()
    freshness.freshness(root, sha)
    assert (root / freshness.STORE_REL).read_bytes() == before


def test_unresolvable_sha_is_not_applicable(fx):
    root, _sha_ = fx
    assert freshness.freshness(root, "not-a-sha")["status"] == "not_applicable"
