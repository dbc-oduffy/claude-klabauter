"""code_intel.freshness through the real dispatch path, against a synthetic example-retrieval-repo store."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.symbol_knowledge import freshness


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _handler():
    handler = ipc._REGISTRY.get("code_intel.freshness") or ipc._lazy_import_and_lookup("code_intel.freshness")
    assert handler is not None
    return handler


def test_op_reports_stale_files_through_the_real_dispatch_path(tmp_path: Path) -> None:
    root = tmp_path / "fx"
    root.mkdir()
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    (root / "b.txt").write_text("fresh\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    (root / ".project-rag").mkdir()
    db = sqlite3.connect(root / freshness.STORE_REL)
    db.execute("CREATE TABLE rc_v1_files (file TEXT, sha256 TEXT, byte_size INTEGER, indexed_at TEXT)")
    db.execute("INSERT INTO rc_v1_files VALUES ('a.txt', 'stale', 0, 't')")
    db.execute("INSERT INTO rc_v1_files VALUES ('b.txt', ?, 0, 't')", (hashlib.sha256(b"fresh\n").hexdigest(),))
    db.execute(
        "CREATE TABLE rc_v1_symbols (symbol_id INTEGER, name TEXT, kind TEXT, scope TEXT, module TEXT,"
        " signature TEXT, docstring TEXT, file TEXT, line INTEGER, line_end INTEGER, is_definition INTEGER)"
    )
    db.commit()
    db.close()
    out = _handler()({"sha": "HEAD"}, repo_root=root)
    assert out["status"] == "ok"
    assert out["files_changed"] == 2 and out["stale_files"] == ["a.txt"] and out["files_reextracted"] == 1
    assert out["queued"] == [] and out["cost"]["spawns"] == 0


def test_op_without_a_store_is_not_applicable(tmp_path: Path) -> None:
    out = _handler()({}, repo_root=tmp_path)
    assert out["status"] == "not_applicable" and out["store"] == freshness.STORE_REL


def test_op_is_compute_only() -> None:
    assert OP_CLASSIFICATION["code_intel.freshness"] is OpClass.COMPUTE_ONLY
