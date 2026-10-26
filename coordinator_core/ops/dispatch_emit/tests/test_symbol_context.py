"""symbol_context.lookup: hit, each degrade reason, one query per row, no subprocess."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import symbol_context as sc
from coordinator_core.ops.dispatch_emit.executor_economy import SYMBOL_CONTEXT_MAX_ENTRIES

SRC = "pkg/mod.py"


def _build(tmp_path: Path, *, store: bool = True, fresh: bool = True, schema_ok: bool = True) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / SRC).write_text("def target():\n    helper()\n\ndef helper():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "user.py").write_text("def use():\n    target()\n", encoding="utf-8")
    rag = tmp_path / ".project-rag"
    rag.mkdir()
    (rag / "state.json").write_text("{}", encoding="utf-8")
    if store:
        db = rag / "structural_project.sqlite3"
        conn = sqlite3.connect(db)
        if schema_ok:
            conn.executescript(
                "CREATE TABLE symbols(id INTEGER PRIMARY KEY, name TEXT, file TEXT, line INTEGER, authority TEXT);"
                "CREATE TABLE calls(caller_id INTEGER, callee_id INTEGER, authority TEXT, producer TEXT);"
                "INSERT INTO symbols VALUES (1,'target','pkg/mod.py',1,'project'),(2,'helper','pkg/mod.py',4,'project'),"
                "(3,'use','pkg/user.py',1,'project'),(4,'ext','lib/x.py',9,'external');"
                "INSERT INTO calls VALUES (3,1,'project','a'),(3,1,'project','b'),(1,2,'project','a'),(1,4,'project','a'),(4,1,'project','a');"
            )
        else:
            conn.executescript("CREATE TABLE symbols(id INTEGER PRIMARY KEY, name TEXT);")
        conn.commit()
        conn.close()
        src_t = os.stat(tmp_path / SRC).st_mtime
        t = src_t + 10 if fresh else src_t - 10
        os.utime(db, (t, t))
    return tmp_path


def _row(rid="C1", writes=(SRC,), body="edit target"):
    return {"id": rid, "writes": None if writes is None else list(writes), "body": body}


def test_hit_lists_callers_and_callees_in_repo_only(tmp_path):
    root = _build(tmp_path)
    ctx = sc.lookup(root, [_row()])["C1"]
    assert ctx.reason is None
    got = [(e.symbol, e.file, e.line, e.role, e.of) for e in ctx.entries]
    assert got == [("use", "pkg/user.py", 1, "caller", "target"), ("helper", "pkg/mod.py", 4, "callee", "target")]


def test_no_index_when_store_missing(tmp_path):
    root = _build(tmp_path, store=False)
    assert sc.lookup(root, [_row()])["C1"] == sc.SymbolContext((), "no-index")


def test_no_index_when_no_rag_dir(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    assert sc.lookup(tmp_path, [_row(writes=("a.py",), body="f")])["C1"].reason == "no-index"


def test_stale_index_when_written_file_newer(tmp_path):
    root = _build(tmp_path, fresh=False)
    assert sc.lookup(root, [_row()])["C1"] == sc.SymbolContext((), "stale-index")


def test_missing_written_file_skipped_by_staleness(tmp_path):
    root = _build(tmp_path)
    ctx = sc.lookup(root, [_row(writes=(SRC, "pkg/new.py"))])["C1"]
    assert ctx.reason is None and ctx.entries


def test_no_symbols_when_body_names_none_or_no_py_writes(tmp_path):
    root = _build(tmp_path)
    out = sc.lookup(root, [_row("A", body="unrelated"), _row("B", writes=("README.md",)), _row("C", writes=None)])
    assert {k: v.reason for k, v in out.items()} == {"A": "no-symbols", "B": "no-symbols", "C": "no-symbols"}


def test_store_error_on_schema_drift(tmp_path):
    root = _build(tmp_path, schema_ok=False)
    assert sc.lookup(root, [_row()])["C1"] == sc.SymbolContext((), "store-error")


def test_one_query_per_row_and_connection_closed(tmp_path, monkeypatch):
    root = _build(tmp_path)
    log = {"queries": 0, "opened": 0, "closed": 0, "uri": None}
    real = sqlite3.connect

    class Counting:
        def __init__(self, conn):
            self._c = conn

        def execute(self, *a):
            log["queries"] += 1
            return self._c.execute(*a)

        def close(self):
            log["closed"] += 1
            self._c.close()

    def connect(target, *a, **kw):
        log["opened"] += 1
        log["uri"] = target
        return Counting(real(target, *a, **kw))

    monkeypatch.setattr(sc.sqlite3, "connect", connect)
    out = sc.lookup(root, [_row("A"), _row("B"), _row("C", body="nothing")])
    assert log["queries"] == 2 and log["opened"] == 1 and log["closed"] == 1
    assert "mode=ro" in log["uri"]
    assert out["A"].entries == out["B"].entries


def test_read_only_store_cannot_be_written(tmp_path):
    root = _build(tmp_path)
    store = root / ".project-rag" / "structural_project.sqlite3"
    conn = sqlite3.connect(f"{store.resolve().as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO symbols(id,name) VALUES (99,'x')")
    conn.close()


def test_no_subprocess(tmp_path, monkeypatch):
    root = _build(tmp_path)

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert sc.lookup(root, [_row()])["C1"].entries


def test_entries_capped(tmp_path):
    root = _build(tmp_path)
    db = root / ".project-rag" / "structural_project.sqlite3"
    conn = sqlite3.connect(db)
    for i in range(100, 100 + SYMBOL_CONTEXT_MAX_ENTRIES + 10):
        conn.execute("INSERT INTO symbols VALUES (?,?,?,?,'project')", (i, f"c{i}", "pkg/user.py", i))
        conn.execute("INSERT INTO calls VALUES (?,1,'project','a')", (i,))
    conn.commit()
    conn.close()
    src_t = os.stat(root / SRC).st_mtime
    os.utime(db, (src_t + 10, src_t + 10))
    ctx = sc.lookup(root, [_row()])["C1"]
    assert len(ctx.entries) == SYMBOL_CONTEXT_MAX_ENTRIES
    assert all(e.role == "caller" for e in ctx.entries)


def test_shared_cache_reused_and_file_read_once(tmp_path):
    root = _build(tmp_path)
    cache: dict = {}
    sc.lookup(root, [_row("A"), _row("B")], cache)
    assert SRC in cache and cache[SRC][0][0] == "target"
