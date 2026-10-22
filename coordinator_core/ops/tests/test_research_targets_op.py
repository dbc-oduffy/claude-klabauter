"""Tests for the research.targets op."""

from __future__ import annotations

import pytest

from coordinator_core import machine_resolver as mr
from coordinator_core.ops.research_targets import _handler


@pytest.fixture
def reg(tmp_path, monkeypatch):
    d = tmp_path / "ml"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    mr._external_ws_cache.clear()
    yield d
    mr._external_ws_cache.clear()


def _register(reg, tmp_path, entries):
    text = ""
    for key, parts, extra in entries:
        p = tmp_path.joinpath(*parts)
        p.mkdir(parents=True)
        text += f'[external_workspaces.{key}]\npath = "{p.as_posix()}"\n{extra}\n'
    (reg / "registry.local.toml").write_text(text, encoding="utf-8")
    mr._external_ws_cache.clear()


def test_rows_carry_purpose_and_topic(reg, tmp_path):
    _register(
        reg,
        tmp_path,
        [("research_a", ("t1", "a"), 'purpose = "research"\ntopic = "Graphs"')],
    )
    out = _handler({})
    assert out["registry_readable"] is True
    (row,) = out["targets"]
    assert row["key"] == "research_a"
    assert row["purpose"] == "research"
    assert row["topic"] == "Graphs"


def test_topic_filter_narrows_casefold(reg, tmp_path):
    _register(
        reg,
        tmp_path,
        [
            ("research_a", ("t1", "a"), 'topic = "Graphs"'),
            ("research_b", ("t2", "b"), 'topic = "Parsing"'),
        ],
    )
    assert [r["key"] for r in _handler({"topic": "graphs"})["targets"]] == ["research_a"]
    assert len(_handler({})["targets"]) == 2
    assert _handler({"topic": "nope"})["targets"] == []


def test_unreadable_registry_reports_false(reg, monkeypatch):
    monkeypatch.setattr(mr, "external_workspace_registry_readable", lambda: False)
    monkeypatch.setattr(mr, "research_targets", lambda: [])
    out = _handler({})
    assert out == {"targets": [], "registry_readable": False}
