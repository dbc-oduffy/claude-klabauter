"""canvas_contradiction: governing sources gathered, contradictions flagged on both sides of each rule."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core import canvas_contradiction as cc

BOARD = "https://claude.ai/artifact/fixture-canvas-001"
REGISTRY = f"""schema: design-registry/1
entries:
  - id: welcome
    board: {BOARD}
    artboards: [a]
    harvest: docs/design/w/harvest/
    sources: ["src/welcome/**"]
    status: designed
"""
SETTLED = """## Thread-settled behaviour

<!--
Threads to settle:
  ignored-in-comment
-->
- [item] [thread:t1] The nudge banner is dismissable.
- [thread:t2] Connect opens the modal in place.
- unrelated line without a thread id

## Other

- [thread:t3] Not in the settled section.
"""
PM = "The Connect button should open a modal on the same page."


def _repo(tmp_path: Path, settled: str | None = SETTLED) -> Path:
    reg = tmp_path / "docs/design/registry.yaml"
    reg.parent.mkdir(parents=True)
    reg.write_text(REGISTRY, encoding="utf-8")
    harvest = tmp_path / "docs/design/w/harvest"
    harvest.mkdir(parents=True)
    (harvest / "harvest.json").write_text("{}", encoding="utf-8")
    if settled is not None:
        (harvest / "SETTLED.md").write_text(settled, encoding="utf-8")
    return tmp_path


def _pm(text: str = PM) -> list:
    return [{"kind": "pm-verbatim", "ref": "pm-verbatim#1", "text": text}]


def test_incident_route_vs_modal_is_flagged():
    found = cc.contradictions("Connect (to /welcome)", _pm())
    assert [f["rule"] for f in found] == ["surface-kind"]
    assert found[0]["source_ref"] == "pm-verbatim#1"


def test_modal_candidate_is_not_flagged():
    assert cc.contradictions("Connect opens the modal", _pm()) == []


def test_inverse_surface_kind_is_flagged():
    src = [{"kind": "canvas-settled", "ref": "x#thread:a", "text": "Connect navigates to /welcome."}]
    found = cc.contradictions("Connect opens a modal.", src)
    assert [f["rule"] for f in found] == ["surface-kind"]


def test_polarity_negation_is_flagged_and_agreement_is_not():
    src = [{"kind": "canvas-settled", "ref": "w#thread:t1", "text": "The nudge banner is dismissable."}]
    found = cc.contradictions("The nudge banner is never dismissable.", src)
    assert [f["rule"] for f in found] == ["polarity"]
    assert cc.contradictions("The nudge banner is dismissable by click.", src) == []


def test_polarity_antonym_pair():
    src = [{"kind": "canvas-settled", "ref": "w#thread:t1", "text": "The nudge banner is dismissable."}]
    found = cc.contradictions("The nudge banner is non-dismissable.", src)
    assert [f["rule"] for f in found] == ["polarity"]


def test_one_shared_word_is_not_polarity():
    src = [{"kind": "canvas-settled", "ref": "w#thread:t1", "text": "The banner is dismissable."}]
    assert cc.contradictions("Do not touch the schema.", src) == []


def test_empty_inputs():
    assert cc.contradictions("", _pm()) == []
    assert cc.contradictions("Connect (to /welcome)", []) == []
    assert cc.contradictions("Connect (to /welcome)", None) == []


def test_governing_sources_reads_pm_and_settled_items(tmp_path):
    root = _repo(tmp_path)
    sizing = {"pm_verbatims": [{"text": PM}]}
    got = cc.governing_sources(root, sizing, ["src/welcome/page.tsx"])
    assert {"kind": "pm-verbatim", "ref": "pm-verbatim#1", "text": PM} in got
    canvas = {s["ref"]: s["text"] for s in got if s["kind"] == "canvas-settled"}
    assert canvas == {
        "welcome#thread:t1": "The nudge banner is dismissable.",
        "welcome#thread:t2": "Connect opens the modal in place.",
    }


def test_governing_sources_end_to_end_flags_settled_negation(tmp_path):
    root = _repo(tmp_path)
    got = cc.governing_sources(root, None, ["src/welcome/page.tsx"])
    found = cc.contradictions("The nudge banner is not dismissable.", got)
    assert [(f["rule"], f["source_ref"]) for f in found] == [("polarity", "welcome#thread:t1")]


def test_governing_sources_degrades_without_raising(tmp_path):
    assert cc.governing_sources(tmp_path, None, ["src/welcome/x.ts"]) == []
    root = _repo(tmp_path / "nosettled", settled=None) if (tmp_path / "nosettled").mkdir() is None else None
    assert cc.governing_sources(root, None, ["src/welcome/x.ts"]) == []
    (tmp_path / "bad").mkdir()
    bad = tmp_path / "bad" / "docs/design"
    bad.mkdir(parents=True)
    (bad / "registry.yaml").write_text("not: [valid\n  - x", encoding="utf-8")
    got = cc.governing_sources(tmp_path / "bad", {"pm_verbatims": [{"text": PM}]}, ["src/welcome/x.ts"])
    assert [s["kind"] for s in got] == ["pm-verbatim"]


def test_no_subprocess_spawned(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    root = _repo(tmp_path)
    got = cc.governing_sources(root, {"pm_verbatims": [{"text": PM}]}, ["src/welcome/page.tsx"])
    assert cc.contradictions("Connect (to /welcome)", got)
