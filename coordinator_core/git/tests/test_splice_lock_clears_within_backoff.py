"""A peer-held index.lock that clears within the splice backoff lands the commit; one that never clears refuses."""

from __future__ import annotations

import pytest

from coordinator_core.git import commit, index_write


class _Sentinel:
    pass


def _patch_prepare(monkeypatch, busy_attempts: int):
    calls = {"n": 0}
    prepared = _Sentinel()

    def fake_prepare(repo, index_updates, **kwargs):
        calls["n"] += 1
        if calls["n"] <= busy_attempts:
            raise index_write.IndexWriteLockBusy("index.lock exists -- a peer holds the index")
        return prepared

    monkeypatch.setattr(index_write, "prepare_splice", fake_prepare)
    return calls, prepared


def test_lock_that_clears_within_the_schedule_lands(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(commit.time, "sleep", pauses.append)
    calls, prepared = _patch_prepare(monkeypatch, busy_attempts=3)

    assert commit._prepare_index_splice("repo", {}, None) is prepared
    assert calls["n"] == 4
    assert pauses == list(commit._SPLICE_LOCK_BACKOFF_SECS[:3])


def test_lock_that_never_clears_refuses_after_the_whole_schedule(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(commit.time, "sleep", pauses.append)
    calls, _ = _patch_prepare(monkeypatch, busy_attempts=10**6)

    with pytest.raises(commit.CommitRefused, match="Safe to retry"):
        commit._prepare_index_splice("repo", {}, None)
    assert calls["n"] == len(commit._SPLICE_LOCK_BACKOFF_SECS) + 1
    assert pauses == list(commit._SPLICE_LOCK_BACKOFF_SECS)


def test_schedule_outlasts_a_peer_hold_but_stays_bounded():
    total = sum(commit._SPLICE_LOCK_BACKOFF_SECS)
    assert 1.0 <= total <= 10.0
