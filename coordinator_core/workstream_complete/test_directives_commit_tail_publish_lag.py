"""Tests for `directives_commit_tail.compute_publish_lag_advisory` and its
`render_final_summary` wiring (DR-335 call site (b)).

Spec backlink: docs/decisions/DR-335-publish-lag-is-surfaced-not-shortened.md

Mirrors `test_directives_commit_tail_push_status.py`'s style: no real git
subprocess, `skew.publish_lag`/`skew.publish_lag_message` monkeypatched
directly so this file stays on the fast tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.workstream_complete import directives_commit_tail as _tail


def test_compute_publish_lag_advisory_returns_none_when_skew_returns_none(monkeypatch):
    monkeypatch.setattr(_tail._skew, "publish_lag", lambda engine_root, source_root: None)
    assert _tail.compute_publish_lag_advisory(Path("/repo")) is None


def test_compute_publish_lag_advisory_returns_message_when_above_threshold(monkeypatch):
    sentinel_lag = object()
    monkeypatch.setattr(_tail._skew, "publish_lag", lambda engine_root, source_root: sentinel_lag)
    monkeypatch.setattr(
        _tail._skew, "publish_lag_message",
        lambda lag, **kw: "Engine lag: 2 commit(s) touching engine code are unpublished (oldest 1.0h)." if lag is sentinel_lag else None,
    )
    message = _tail.compute_publish_lag_advisory(Path("/repo"))
    assert message is not None
    assert "2 commit(s)" in message


def test_compute_publish_lag_advisory_never_raises(monkeypatch):
    def boom(engine_root, source_root):
        raise RuntimeError("boom")

    monkeypatch.setattr(_tail._skew, "publish_lag", boom)
    assert _tail.compute_publish_lag_advisory(Path("/repo")) is None


def test_render_final_summary_stays_silent_with_no_publish_lag():
    text = _tail.render_final_summary(work_done="did stuff", pushed="yes")
    assert "Publish lag" not in text


def test_render_final_summary_surfaces_publish_lag_line():
    text = _tail.render_final_summary(
        work_done="did stuff",
        pushed="yes",
        publish_lag="Engine lag: 2 commit(s) touching engine code are unpublished (oldest 1.0h).",
    )
    assert "**Publish lag:** Engine lag: 2 commit(s)" in text
