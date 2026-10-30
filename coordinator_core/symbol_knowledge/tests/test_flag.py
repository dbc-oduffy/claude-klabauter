"""Tests for coordinator_core.symbol_knowledge.flag: real git objects, a real symtab, 0 spawns."""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.symbol_knowledge import contract, flag, sidecar

pytestmark = pytest.mark.spawns_process

PARENT_SRC = (
    "def alpha():\n    return 1\n\n\n"
    "def _hidden():\n    return 2\n\n\n"
    "class Box:\n    def size(self):\n        return 3\n\n    def other(self):\n        return 4\n"
)
PREFIX = "scip-python python demo 0.1 `pkg.mod`/"
ALPHA = PREFIX + "alpha()."
HIDDEN = PREFIX + "_hidden()."
SIZE = PREFIX + "Box#size()."
BOX = PREFIX + "Box#"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _span(src: str, needle: str, end_marker: str) -> "tuple[int, int]":
    start = src.index(needle)
    end = src.index(end_marker, start) + len(end_marker)
    return len(src[:start].encode()), len(src[:end].encode())


def _symtab(graph_dir: Path, rows: "list[tuple[str, str, int, int]]") -> Path:
    graph_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(graph_dir / flag.SYMTAB_NAME)
    db.execute(
        "CREATE TABLE symtab (scip_symbol TEXT PRIMARY KEY, symbol_id TEXT NOT NULL, file TEXT NOT NULL,"
        " byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX idx_symtab_file ON symtab(file)")
    for sym, kind, start, end in rows:
        db.execute("INSERT INTO symtab VALUES (?,?,?,?,?,?,?)", (sym, sym, "pkg/mod.py", start, end, kind, sym))
    db.commit()
    db.close()
    (graph_dir / "graph.db").write_bytes(b"")
    return graph_dir / "graph.db"


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "pkg" / "mod.py").write_text(PARENT_SRC, encoding="utf-8", newline="\n")
    (root / "pkg" / "other.txt").write_text("x\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "parent")
    rows = [
        (ALPHA, "function", *_span(PARENT_SRC, "def alpha", "return 1")),
        (HIDDEN, "function", *_span(PARENT_SRC, "def _hidden", "return 2")),
        (BOX, "class", *_span(PARENT_SRC, "class Box", "return 4")),
        (SIZE, "method", *_span(PARENT_SRC, "def size", "return 3")),
        (PREFIX + "Box#other().", "method", *_span(PARENT_SRC, "def other", "return 4")),
        (PREFIX + "__init__:", "namespace", 0, len(PARENT_SRC.encode())),
    ]
    graph_db = _symtab(tmp_path / "idx" / "root", rows)
    return root, graph_db


def _commit_change(root: Path, new_src: str) -> str:
    (root / "pkg" / "mod.py").write_text(new_src, encoding="utf-8", newline="\n")
    _git(root, "commit", "-qam", "change")
    return _git(root, "rev-parse", "HEAD")


class _NoSpawn:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = subprocess.Popen

        def counting(*a, **k):
            self.count += 1
            return real(*a, **k)

        monkeypatch.setattr(subprocess, "Popen", counting)


def test_changed_function_body_flags_one_public_symbol_with_zero_spawns(repo, monkeypatch):
    root, graph_db = repo
    sha = _commit_change(root, PARENT_SRC.replace("return 1", "return 11"))
    spawns = _NoSpawn(monkeypatch)
    start = time.process_time()
    flagged = contract.flag_symbols(root / ".git", sha, graph_db)
    elapsed_ms = (time.process_time() - start) * 1000
    assert spawns.count == 0
    assert flagged == [contract.FlaggedSymbol(ALPHA, "pkg/mod.py", "net-new", True)]
    assert elapsed_ms < 50


def test_private_symbol_change_is_not_flagged(repo):
    root, graph_db = repo
    sha = _commit_change(root, PARENT_SRC.replace("return 2", "return 22"))
    assert flag.flag_symbols(root / ".git", sha, graph_db) == []


def test_method_change_flags_method_not_enclosing_class(repo):
    root, graph_db = repo
    sha = _commit_change(root, PARENT_SRC.replace("return 3", "return 33"))
    assert [f.qualified_name for f in flag.flag_symbols(root / ".git", sha, graph_db)] == [SIZE]


def test_insertion_inside_body_flags_symbol(repo):
    root, graph_db = repo
    sha = _commit_change(root, PARENT_SRC.replace("    return 1\n", "    x = 0\n    return 1\n"))
    assert [f.qualified_name for f in flag.flag_symbols(root / ".git", sha, graph_db)] == [ALPHA]


def test_insertion_between_symbols_flags_nothing(repo):
    root, graph_db = repo
    sha = _commit_change(root, PARENT_SRC.replace("return 2\n\n\n", "return 2\n\n\nNEW = 1\n\n\n"))
    assert flag.flag_symbols(root / ".git", sha, graph_db) == []


def test_kind_is_adjust_when_sidecar_has_entry(repo):
    root, graph_db = repo
    sidecar.write_sidecar(root, {ALPHA: sidecar.make_entry("def", "s1")})
    sha = _commit_change(root, PARENT_SRC.replace("return 1", "return 11"))
    assert [(f.qualified_name, f.kind) for f in flag.flag_symbols(root / ".git", sha, graph_db)] == [(ALPHA, "adjust")]


def test_non_python_change_flags_nothing(repo):
    root, graph_db = repo
    (root / "pkg" / "other.txt").write_text("y\n", encoding="utf-8")
    _git(root, "commit", "-qam", "txt")
    flagged, detail = flag.flag_symbols_detail(root / ".git", _git(root, "rev-parse", "HEAD"), graph_db)
    assert flagged == [] and "no existing python file" in detail


def test_missing_symtab_returns_empty_with_reason(repo, tmp_path):
    root, _ = repo
    sha = _commit_change(root, PARENT_SRC.replace("return 1", "return 11"))
    flagged, detail = flag.flag_symbols_detail(root / ".git", sha, tmp_path / "nowhere" / "graph.db")
    assert flagged == [] and detail == "SCIP symtab absent"


def test_corrupt_symtab_never_raises(repo):
    root, graph_db = repo
    (graph_db.parent / flag.SYMTAB_NAME).write_bytes(b"not a database" * 50)
    sha = _commit_change(root, PARENT_SRC.replace("return 1", "return 11"))
    flagged, detail = flag.flag_symbols_detail(root / ".git", sha, graph_db)
    assert flagged == [] and "unreadable" in detail


def test_root_commit_has_no_parent(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    graph_db = _symtab(tmp_path / "idx", [])
    flagged, detail = flag.flag_symbols_detail(root / ".git", _git(root, "rev-parse", "HEAD"), graph_db)
    assert flagged == [] and "no parent" in detail


@pytest.mark.parametrize(
    "symbol, expected",
    [
        (ALPHA, True),
        (HIDDEN, False),
        (PREFIX + "Box#_priv().", False),
        ("scip-python python demo 0.1 `pkg._mod`/f().", False),
        ("scip-python python demo 0.1 `pkg.__init__`/f().", True),
        ("garbage", False),
    ],
)
def test_is_public(symbol, expected):
    assert flag.is_public(symbol) is expected
