"""A sibling slice's stamped context seeds its requirement register as a `relayed` row."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as fc
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.sizing_intake_contract import EM_BRIEF_KEY, EM_CONTEXT_PATHS_KEY


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _slice(repo, name, source, **analysis_extra):
    rel = f"state/sizings/2026-10-01-{name}.yaml"
    analysis = {"ask_parent": fc.ask_parent_marker("ask-parent-x", name), **analysis_extra}
    if source:
        analysis[fc.SLICE_SOURCE_KEY] = source
    doc = {
        "schema": "sizing-object", "name": name, "intent": f"intent of {name}", "intent_source": "em-elaborated",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "draft",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "em_analysis": analysis,
    }
    (repo / rel).write_text(yaml.safe_dump(doc), encoding="utf-8")
    run_dir = rl.start(repo, name)
    rl.bind(repo, name, rel)
    return rel, run_dir


def _rows(repo, rel):
    return rr.read_register((repo / rel).read_text(encoding="utf-8")).rows


def test_a_relayed_slice_with_context_seeds_intent_and_context_rows(repo):
    rel, run_dir = _slice(
        repo, "rel-ctx", "relayed",
        **{EM_BRIEF_KEY: "concrete host bug: X", EM_CONTEXT_PATHS_KEY: "docs/a.md\nb/c.md"},
    )
    rows = _rows(repo, rel)
    assert [r["anchor"] for r in rows] == ["relayed", "relayed"]
    assert rows[0]["source_text"] == "intent of rel-ctx"
    assert rows[1]["source_text"] == "concrete host bug: X\nContext files: docs/a.md\nContext files: b/c.md"
    view = (run_dir / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert "## R2 (relayed)" in view and "> concrete host bug: X" in view


def test_a_pm_slice_with_context_gets_an_ask_row_and_a_relayed_row(repo):
    rel, _ = _slice(repo, "pm-ctx", "pm", **{EM_BRIEF_KEY: "host bug: Y"})
    assert [(r["source_text"], r["anchor"]) for r in _rows(repo, rel)] == [
        ("intent of pm-ctx", "ask"), ("host bug: Y", "relayed"),
    ]


def test_paths_alone_seed_a_context_row(repo):
    rel, _ = _slice(repo, "paths-only", "pm", **{EM_CONTEXT_PATHS_KEY: "docs/a.md"})
    assert [r["source_text"] for r in _rows(repo, rel)] == ["intent of paths-only", "Context files: docs/a.md"]


def test_a_slice_without_context_seeds_one_row(repo):
    rel, _ = _slice(repo, "plain", "relayed")
    assert [(r["source_text"], r["anchor"]) for r in _rows(repo, rel)] == [("intent of plain", "relayed")]


def test_a_slice_holding_assigned_rows_seeds_no_context_row(repo):
    sizing = {
        "intent": "i",
        "em_analysis": {EM_BRIEF_KEY: "ctx"},
        rr.REGISTER_KEY: {rr.ROWS_KEY: [{"id": "R4", "source_text": "assigned"}]},
    }
    assert rl._slice_entries(sizing) == []
