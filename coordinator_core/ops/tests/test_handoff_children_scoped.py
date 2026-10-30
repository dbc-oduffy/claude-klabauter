"""Tests for `handoff_children.has_live_children_among_records`: the live set is
the caller's explicit `scope`, never the corpus, and no path outside it is read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import coordinator_core.dag as dag
from coordinator_core.ops import handoff_children
from coordinator_core.ops.handoff_children import (
    CONCLUSION_EDGE_KINDS,
    has_live_children_among_records,
)
pytestmark = [pytest.mark.cadence]


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state" / "handoffs").mkdir(parents=True)
    return repo


def _seed(repo: Path, name: str, predecessor: str | None = None) -> str:
    path = repo / "state" / "handoffs" / name
    fm = f'title: "{name}"\ncreated: 2026-01-01\nstatus: open\n'
    if predecessor:
        fm += f'predecessor: "{predecessor}"\n'
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    return str(path)


def _call(repo: Path, candidate: str, scope: list[str], **kw) -> dict:
    return asyncio.run(
        has_live_children_among_records(
            candidate,
            repo / ".git",
            scope=scope,
            edge_kinds=CONCLUSION_EDGE_KINDS,
            **kw,
        )
    )


def _pair(tmp_path: Path) -> tuple[Path, str, str]:
    repo = _make_repo(tmp_path)
    a = _seed(repo, "a.md")
    b = _seed(repo, "b.md", predecessor="state/handoffs/a.md")
    return repo, a, b


def test_successor_in_scope_is_referenced(tmp_path):
    repo, a, b = _pair(tmp_path)
    result = _call(repo, a, [a, b])
    assert result["referenced"] is True
    assert result["exit_code"] == 0
    assert [Path(c).name for c in result["children"]] == ["b.md"]


def test_successor_outside_scope_is_not_seen(tmp_path):
    repo, a, _b = _pair(tmp_path)
    result = _call(repo, a, [a])
    assert result == {"referenced": False, "children": [], "exit_code": 1}


def test_empty_scope_answers_not_referenced(tmp_path):
    repo, a, _b = _pair(tmp_path)
    result = _call(repo, a, [])
    assert result == {"referenced": False, "children": [], "exit_code": 1}


def test_archive_resident_scope_entries_are_dropped(tmp_path):
    repo, a, _b = _pair(tmp_path)
    arch = repo / "archive" / "handoffs" / "c.md"
    arch.parent.mkdir(parents=True)
    arch.write_text(
        '---\ntitle: "c"\nstatus: open\npredecessor: "state/handoffs/a.md"\n---\n',
        encoding="utf-8",
    )
    result = _call(repo, a, [a, str(arch)])
    assert result["referenced"] is False


def test_bad_candidate_is_indeterminate(tmp_path):
    repo, a, b = _pair(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntitle: x\n---\n", encoding="utf-8")
    result = _call(repo, str(outside), [a, b])
    assert result["exit_code"] == 2
    assert "referenced" not in result


def test_never_enumerates_live_set_nor_reads_outside_scope(tmp_path, monkeypatch):
    repo, a, b = _pair(tmp_path)
    stray = _seed(repo, "stray.md", predecessor="state/handoffs/a.md")

    async def _boom(*_a, **_k):
        raise AssertionError("_enumerate_live_set must not be called")

    monkeypatch.setattr(handoff_children, "_enumerate_live_set", _boom)

    read: list[str] = []
    real = dag._read_meta

    def _spy(path, *a, **k):
        read.append(str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(dag, "_read_meta", _spy)
    monkeypatch.setattr(handoff_children, "_read_meta", _spy)

    result = _call(repo, a, [a, b])
    assert result["referenced"] is True
    assert [Path(c).name for c in result["children"]] == ["b.md"]
    assert read, "spy never fired; the read-path assertion would be vacuous"
    in_scope = {str(Path(p).resolve()) for p in (a, b)}
    assert {str(Path(p).resolve()) for p in read} <= in_scope
    assert str(Path(stray).resolve()) not in {str(Path(p).resolve()) for p in read}
