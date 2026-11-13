"""Tests for coordinator_core.ops.rag_freshness."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import rag_freshness as rf


def _verdict(state, remedy=("project-rag", "refresh")):
    return lambda repo: rf.FreshnessVerdict(state=state, remedy=remedy, source="stub")


@pytest.mark.parametrize("state", ["stale", "absent"])
def test_stale_or_absent_queues_one_refresh(tmp_path, state):
    calls = []
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict(state), spawn_fn=lambda a, c: calls.append((a, c)), marker_root=tmp_path / "m"
    )
    assert out["state"] == state and out["refresh_queued"] is True
    assert calls == [(["project-rag", "refresh"], str(tmp_path))]


@pytest.mark.parametrize("state", ["fresh", "unknown"])
def test_fresh_or_unknown_queues_nothing(tmp_path, state):
    calls = []
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict(state), spawn_fn=lambda a, c: calls.append(a), marker_root=tmp_path / "m"
    )
    assert out["refresh_queued"] is False and calls == []


def test_default_seam_is_unknown_and_spawns_nothing(tmp_path):
    calls = []
    out = rf.freshness_precondition(str(tmp_path), spawn_fn=lambda a, c: calls.append(a), marker_root=tmp_path / "m")
    assert out["state"] == "unknown" and out["refresh_queued"] is False and calls == []


def test_live_marker_suppresses_spawn_and_expired_marker_allows_one(tmp_path):
    calls = []
    kw = dict(verdict_fn=_verdict("stale"), spawn_fn=lambda a, c: calls.append(a), marker_root=tmp_path / "m")
    assert rf.freshness_precondition(str(tmp_path), now=1000.0, **kw)["refresh_queued"] is True
    live = rf.freshness_precondition(str(tmp_path), now=1000.0 + rf.MARKER_TTL_SECONDS - 1, **kw)
    assert live["refresh_queued"] is False and "already queued" in live["message"]
    assert len(calls) == 1
    expired = rf.freshness_precondition(str(tmp_path), now=1000.0 + rf.MARKER_TTL_SECONDS + 1, **kw)
    assert expired["refresh_queued"] is True and len(calls) == 2


def test_missing_settings_home_is_typed_refusal_failing_open(tmp_path, monkeypatch):
    import coordinator_core._settings_home as sh

    def boom():
        raise RuntimeError("unset")

    monkeypatch.setattr(sh, "settings_home", boom)
    with pytest.raises(rf.FreshnessRefusal) as ei:
        rf.queue_refresh(str(tmp_path), ["x"], spawn_fn=lambda a, c: None)
    assert ei.value.key == rf.SETTINGS_HOME_KEY and rf.SETTINGS_HOME_KEY in str(ei.value)
    out = rf.freshness_precondition(str(tmp_path), verdict_fn=_verdict("stale"), spawn_fn=lambda a, c: None)
    assert out["refresh_queued"] is False and rf.SETTINGS_HOME_KEY in out["message"]


def test_spawn_failure_fails_open_and_clears_marker(tmp_path):
    def bad(a, c):
        raise OSError("nope")

    root = tmp_path / "m"
    out = rf.freshness_precondition(str(tmp_path), verdict_fn=_verdict("absent"), spawn_fn=bad, marker_root=root)
    assert out["refresh_queued"] is False and "spawn failed" in out["message"]
    calls = []
    again = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict("absent"), spawn_fn=lambda a, c: calls.append(a), marker_root=root
    )
    assert again["refresh_queued"] is True and len(calls) == 1


def test_verdict_failure_fails_open(tmp_path):
    def bad(repo):
        raise RuntimeError("down")

    out = rf.freshness_precondition(str(tmp_path), verdict_fn=bad, marker_root=tmp_path / "m")
    assert out["state"] == "unknown" and out["refresh_queued"] is False


def test_stale_without_remedy_queues_nothing(tmp_path):
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict("stale", remedy=()), spawn_fn=lambda a, c: pytest.fail("spawned"), marker_root=tmp_path / "m"
    )
    assert out["refresh_queued"] is False and "no remedy" in out["message"]


def test_detach_kwargs_branches():
    assert rf.detach_kwargs("posix") == {"start_new_session": True}
    flags = rf.detach_kwargs("nt")["creationflags"]
    assert flags & 0x00000008 and flags & 0x00000200 and "start_new_session" not in rf.detach_kwargs("nt")


def test_default_spawn_passes_detach_kwargs(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
    rf.default_spawn_fn(["a", "b"], str(tmp_path))
    assert seen["argv"] == ["a", "b"] and seen["cwd"] == str(tmp_path)
    assert seen["stdin"] is subprocess.DEVNULL
    assert all(seen[k] == v for k, v in rf.detach_kwargs().items())
