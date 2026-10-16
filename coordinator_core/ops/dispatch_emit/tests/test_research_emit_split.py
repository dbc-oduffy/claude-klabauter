"""A multi-source research emit over the Workflow script cap splits into one script and receipt per leg (preflight and notebooklm share one)."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import pipeline_compose
from coordinator_core.ops.dispatch_emit.tests.test_research_emit_script_cap import VENDORED, _emit

_SOURCES = ["web", "notebooklm"]


def test_under_the_cap_the_reply_carries_no_parts(monkeypatch, tmp_path, capsys):
    _, reply = _emit(monkeypatch, tmp_path, capsys, VENDORED, _SOURCES, 2)
    assert "parts" not in reply
    assert reply["path"].endswith(".workflow.mjs")


def test_over_the_cap_it_emits_one_script_per_pipeline(monkeypatch, tmp_path, capsys):
    whole, whole_reply = _emit(monkeypatch, tmp_path / "whole", capsys, VENDORED, _SOURCES, 2)
    cap = len(whole.encode("utf-8")) - 1
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", cap)
    _, reply = _emit(monkeypatch, tmp_path / "split", capsys, VENDORED, _SOURCES, 2)

    parts = reply["parts"]
    assert list(whole_reply["pipelines"]) == ["nlm-preflight", "web", "notebooklm"]
    assert [p["pipeline"] for p in parts] == ["nlm-preflight+notebooklm", "web"]
    assert [p["part"] for p in parts] == [f"{i}/{len(parts)}" for i in range(1, len(parts) + 1)]
    assert len(parts) > 1
    assert reply["path"] == parts[0]["path"] and reply["sha256"] == parts[0]["sha256"]
    texts = []
    for p in parts:
        path = Path(p["path"])
        assert path.name.endswith(f"-{p['pipeline']}.workflow.mjs")
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert len(text.encode("utf-8")) <= cap
        assert p["sha256"] and p["receipt"]
        texts.append(text)
    assert ["research.close" in t for t in texts] == [False] * (len(parts) - 1) + [True]
    assert reply["next_action"]["params"]["segment_dirs"] == list(whole_reply["pipelines"])[1:]
