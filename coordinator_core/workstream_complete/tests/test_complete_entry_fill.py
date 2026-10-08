"""apply threads decided title/nature/prose into the d-complete-entry scaffold,
and refuses (naming the key) rather than leaving a PLACEHOLDER entry."""

from __future__ import annotations

import subprocess
from types import ModuleType

import pytest

from coordinator_core.ops import coordinator_complete_entry as cce
from coordinator_core.workstream_complete import apply as ws_apply

_LOE = "loe:\n  agent_dispatches: null\n  opus_dispatches: null\n  em_tokens: null\n  tshirt: null"
_PROSE = "Shipped the thing. It matters because of reasons."


def _scaffold(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    entry = tmp_path / "entry.md"
    cce._write_entry(str(entry), "sid-123456", "", "chain-x", False, _LOE, "", "2026-10-08", "")
    assert "PLACEHOLDER" in entry.read_text(encoding="utf-8")
    return entry


def _run(monkeypatch, tmp_path, entry, decisions):
    mod = ModuleType("fake_entry")
    mod.main = lambda argv: print(str(entry)) or 0
    monkeypatch.setattr(ws_apply, "_load_cli_module", lambda name: mod)
    monkeypatch.setattr(ws_apply, "resolve_repo_root", lambda: tmp_path)
    monkeypatch.setattr(ws_apply, "compute_repo_identity_gate", lambda *a, **k: {"verdict": "OK"})
    monkeypatch.setattr(ws_apply.directives_review, "record_gate_verdict_if_passed", lambda *a, **k: None)
    directive = {
        "id": "d-complete-entry",
        "cli": "coordinator-complete-entry",
        "args": [],
        "depends_on": None,
        "already_satisfied": False,
    }
    return ws_apply._execute_directives([directive], [], decisions)


def test_decisions_fill_title_nature_prose(monkeypatch, tmp_path):
    entry = _scaffold(tmp_path)
    decisions = {
        "title": "Built the thing",
        "prose": _PROSE,
        "completion-nature-classification": {"disposition": "roadmap"},
    }
    code, report = _run(monkeypatch, tmp_path, entry, decisions)
    text = entry.read_text(encoding="utf-8")
    assert report["failed"] == []
    assert "PLACEHOLDER" not in text
    assert "NATURE-INFER" not in text and "<!-- PROSE" not in text
    assert 'title: "Built the thing"' in text
    assert "nature: roadmap" in text and "nature_inferred: false" in text
    assert _PROSE in text


def test_missing_prose_is_refused_with_named_key(monkeypatch, tmp_path):
    entry = _scaffold(tmp_path)
    decisions = {"title": "Built the thing", "nature": "roadmap"}
    code, report = _run(monkeypatch, tmp_path, entry, decisions)
    assert [f["id"] for f in report["failed"]] == ["d-complete-entry"]
    assert "prose" in report["failed"][0]["error"]
    assert "d-complete-entry" not in report["landed"]
    assert "PLACEHOLDER" in entry.read_text(encoding="utf-8")  # untouched, not half-filled


def test_authored_entry_is_left_alone(monkeypatch, tmp_path):
    entry = _scaffold(tmp_path)
    decisions = {"title": "T", "nature": "bugfix", "prose": _PROSE}
    _run(monkeypatch, tmp_path, entry, decisions)
    first = entry.read_text(encoding="utf-8")
    code, report = _run(monkeypatch, tmp_path, entry, {})
    assert report["failed"] == [] and entry.read_text(encoding="utf-8") == first
