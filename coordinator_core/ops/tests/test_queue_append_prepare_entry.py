"""Tests for queue_append.prepare_queue_entry: validate-only, no write, shared with append."""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.queue_append import append_queue_entry, prepare_queue_entry

_KW = dict(
    title="Prepare entry probe",
    body="line one\\nline two",
    status="open",
    created="2026-10-22",
    from_repo="probe-em",
    surface="queue",
    proposed_action="none",
    source="probe",
    risk="Low.",
)


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))


def test_prepare_writes_nothing(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    prepared = prepare_queue_entry("debt-backlog", session_id="s1", **_KW)
    assert not os.path.exists(prepared.out_path)
    assert list(tmp_path.rglob("*.yaml")) == []
    assert not os.path.isdir(os.path.dirname(prepared.out_path))
    assert prepared.digest12 and prepared.slug and prepared.yaml_content


def test_append_matches_prepare_bytes_and_path(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    prepared = prepare_queue_entry("debt-backlog", session_id="s1", **_KW)
    result = append_queue_entry("debt-backlog", session_id="s1", **_KW)
    assert result["out_path"] == prepared.out_path
    assert result["slug"] == prepared.slug
    with open(result["out_path"], encoding="utf-8", newline="") as fh:
        assert fh.read() == prepared.yaml_content


def test_prepare_validates_like_append(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    bad = dict(_KW, status="not-a-status")
    with pytest.raises(ValueError):
        prepare_queue_entry("debt-backlog", session_id="s1", **bad)
    with pytest.raises(ValueError):
        append_queue_entry("debt-backlog", session_id="s1", **bad)
    assert list(tmp_path.rglob("*.yaml")) == []
