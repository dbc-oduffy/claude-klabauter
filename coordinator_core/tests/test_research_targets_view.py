"""Tests for machine_resolver.research_targets / resolve_research_target."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import machine_resolver as mr


@pytest.fixture
def reg(tmp_path, monkeypatch):
    d = tmp_path / "ml"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    mr._external_ws_cache.clear()
    yield d
    mr._external_ws_cache.clear()


def _write(reg, text):
    (reg / "registry.local.toml").write_text(text, encoding="utf-8")
    mr._external_ws_cache.clear()


def _ws(tmp_path, *parts):
    p = tmp_path.joinpath(*parts)
    p.mkdir(parents=True)
    return p


def test_research_prefixed_key_without_purpose_listed_as_research(reg, tmp_path):
    ws = _ws(tmp_path, "project-rag", "Glean")
    _write(reg, f'[external_workspaces.research_glean]\npath = "{ws.as_posix()}"\n')
    rows = mr.research_targets()
    assert [r["key"] for r in rows] == ["research_glean"]
    assert rows[0]["purpose"] == "research"
    assert rows[0]["path"] == Path(ws).resolve().as_posix()


def test_non_research_key_with_research_purpose_listed(reg, tmp_path):
    ws = _ws(tmp_path, "t", "x")
    _write(reg, f'[external_workspaces.other]\npath = "{ws.as_posix()}"\npurpose = " research "\n')
    assert [r["key"] for r in mr.research_targets()] == ["other"]


def test_non_research_key_without_purpose_not_listed(reg, tmp_path):
    ws = _ws(tmp_path, "t", "x")
    _write(reg, f'[external_workspaces.other]\npath = "{ws.as_posix()}"\n')
    assert mr.research_targets() == []


def test_registered_topic_wins(reg, tmp_path):
    ws = _ws(tmp_path, "t", "x")
    _write(reg, f'[external_workspaces.research_x]\npath = "{ws.as_posix()}"\ntopic = "rag"\n')
    row = mr.research_targets()[0]
    assert (row["topic"], row["topic_source"]) == ("rag", "registered")


def test_absent_topic_falls_back_to_parent_dir_name(reg, tmp_path):
    ws = _ws(tmp_path, "project-rag", "Glean")
    _write(reg, f'[external_workspaces.research_x]\npath = "{ws.as_posix()}"\n')
    row = mr.research_targets()[0]
    assert (row["topic"], row["topic_source"]) == ("project-rag", "path")


def test_rows_sorted_by_key(reg, tmp_path):
    a, b = _ws(tmp_path, "t", "a"), _ws(tmp_path, "t", "b")
    _write(
        reg,
        f'[external_workspaces.research_b]\npath = "{b.as_posix()}"\n'
        f'[external_workspaces.research_a]\npath = "{a.as_posix()}"\n',
    )
    assert [r["key"] for r in mr.research_targets()] == ["research_a", "research_b"]


def test_resolve_exact_short_and_miss(reg, tmp_path):
    ws = _ws(tmp_path, "t", "glean")
    other = _ws(tmp_path, "t", "plain")
    _write(
        reg,
        f'[external_workspaces.research_glean]\npath = "{ws.as_posix()}"\n'
        f'[external_workspaces.glean]\npath = "{other.as_posix()}"\n',
    )
    expected = Path(ws).resolve().as_posix()
    assert mr.resolve_research_target("research_glean") == expected
    assert mr.resolve_research_target("nope") is None
    _write(reg, f'[external_workspaces.research_glean]\npath = "{ws.as_posix()}"\n')
    assert mr.resolve_research_target("glean") == expected


def test_missing_dir_entry_absent_from_both(reg, tmp_path):
    _write(reg, f'[external_workspaces.research_gone]\npath = "{(tmp_path / "nope").as_posix()}"\n')
    assert mr.research_targets() == []
    assert mr.resolve_research_target("gone") is None
    assert mr.resolve_research_target("research_gone") is None
