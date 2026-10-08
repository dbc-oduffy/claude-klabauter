"""Tests coordinator/lib/projectrag_planes.py: plane table resolution and decline-reason handling."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[2] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from projectrag_planes import (  # noqa: E402
    PlaneDecline,
    PlaneDeclineReason,
    PlaneTable,
    _ro_connect,
    locate_table,
    resolve_plane_root,
)


def _make_db(path: Path, tables: dict[str, int]) -> None:
    conn = sqlite3.connect(str(path))
    for table, row_count in tables.items():
        conn.execute(f"create table {table} (id integer)")
        for i in range(row_count):
            conn.execute(f"insert into {table} (id) values (?)", (i,))
    conn.commit()
    conn.close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return tmp_path


def test_root_missing_is_a_typed_decline(repo: Path):
    result = locate_table(repo, "md_symbols")
    assert isinstance(result, PlaneDecline)
    assert result.reason is PlaneDeclineReason.ROOT_MISSING
    assert result.file is None


def test_plane_file_missing_is_a_typed_decline(repo: Path):
    (repo / "Saved" / "ProjectRag").mkdir(parents=True)
    result = locate_table(repo, "md_symbols")
    assert isinstance(result, PlaneDecline)
    assert result.reason is PlaneDeclineReason.FILE_MISSING


def test_table_absent_from_every_plane_file_is_a_typed_decline(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    _make_db(plane_root / "graph.db", {"other_table": 3})
    _make_db(plane_root / "structural_project.sqlite3", {"symbols": 2})

    result = locate_table(repo, "md_symbols")
    assert isinstance(result, PlaneDecline)
    assert result.reason is PlaneDeclineReason.TABLE_ABSENT


def test_table_found_in_unexpected_file_is_a_typed_decline(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    _make_db(plane_root / "graph.db", {"md_symbols": 5})
    _make_db(plane_root / "structural_project.sqlite3", {})

    result = locate_table(
        repo, "md_symbols", expected_file="structural_project.sqlite3"
    )
    assert isinstance(result, PlaneDecline)
    assert result.reason is PlaneDeclineReason.TABLE_FOUND_ELSEWHERE
    assert result.file is not None and result.file.name == "graph.db"
    assert result.row_count == 5


def test_table_present_and_empty_is_a_typed_decline(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    _make_db(plane_root / "structural_project.sqlite3", {"doc_links": 0})

    result = locate_table(repo, "doc_links")
    assert isinstance(result, PlaneDecline)
    assert result.reason is PlaneDeclineReason.TABLE_EMPTY
    assert result.row_count == 0


def test_table_located_with_rows_is_a_success(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    _make_db(plane_root / "structural_project.sqlite3", {"doc_links": 42})

    result = locate_table(
        repo, "doc_links", expected_file="structural_project.sqlite3"
    )
    assert isinstance(result, PlaneTable)
    assert result.row_count == 42
    assert result.file.name == "structural_project.sqlite3"


def test_no_expected_file_still_resolves_by_probe(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    _make_db(plane_root / "graph.db", {"md_symbols": 7})

    result = locate_table(repo, "md_symbols")
    assert isinstance(result, PlaneTable)
    assert result.file.name == "graph.db"
    assert result.row_count == 7


def test_resolve_plane_root_absent(repo: Path):
    assert resolve_plane_root(repo) is None


def test_resolve_plane_root_present(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    assert resolve_plane_root(repo) == plane_root


def test_read_only_uses_mode_ro_uri(repo: Path):
    plane_root = repo / "Saved" / "ProjectRag"
    plane_root.mkdir(parents=True)
    _make_db(plane_root / "graph.db", {"md_symbols": 1})

    conn = _ro_connect(plane_root / "graph.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("insert into md_symbols (id) values (99)")
    finally:
        conn.close()
