"""Tests for coordinator_core.warm.skew's publish-lag surface (DR-335).

Spec backlink: docs/decisions/DR-335-publish-lag-is-surfaced-not-shortened.md

All git interaction is monkeypatched -- these tests never spawn a real
`git` process, so no `spawns_process`/`cadence` marker is needed and the
suite stays on the fast tier.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.warm import skew


def _write_stamp(engine_root: Path, sha: str = "abc123") -> None:
    stamp = engine_root / "coordinator_core" / skew.ENGINE_STAMP_FILENAME
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(f"sha:{sha}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# read_engine_stamp_sha
# ---------------------------------------------------------------------------


def test_read_engine_stamp_sha_absent_file_returns_none(tmp_path):
    assert skew.read_engine_stamp_sha(tmp_path) is None


def test_read_engine_stamp_sha_malformed_returns_none(tmp_path):
    stamp = tmp_path / "coordinator_core" / skew.ENGINE_STAMP_FILENAME
    stamp.parent.mkdir(parents=True)
    stamp.write_text("not-a-sha-line\n", encoding="utf-8")
    assert skew.read_engine_stamp_sha(tmp_path) is None


def test_read_engine_stamp_sha_reads_bare_sha(tmp_path):
    _write_stamp(tmp_path, "deadbeef")
    assert skew.read_engine_stamp_sha(tmp_path) == "deadbeef"


# ---------------------------------------------------------------------------
# publish_lag
# ---------------------------------------------------------------------------


def test_publish_lag_no_stamp_returns_none(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        skew.subprocess, "run",
        lambda *a, **k: calls.append(a) or pytest.fail("git must not be spawned with no stamp"),
    )
    assert skew.publish_lag(tmp_path, tmp_path) is None
    assert calls == []


def test_publish_lag_unresolvable_sha_returns_none(tmp_path, monkeypatch):
    _write_stamp(tmp_path)

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["git", "-C"]
        return SimpleNamespace(returncode=128, stdout="", stderr="unknown revision")

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    assert skew.publish_lag(tmp_path, tmp_path) is None


def test_publish_lag_below_threshold_returns_lag_with_none_age_when_zero_behind(tmp_path, monkeypatch):
    _write_stamp(tmp_path)

    def fake_run(cmd, **kwargs):
        assert "rev-list" in cmd
        return SimpleNamespace(returncode=0, stdout="0\n", stderr="")

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    lag = skew.publish_lag(tmp_path, tmp_path)
    assert lag is not None
    assert lag.engine_commits_behind == 0
    assert lag.age_minutes is None
    assert skew.publish_lag_message(lag) is None


def test_publish_lag_above_threshold_computes_age_and_message(tmp_path, monkeypatch):
    _write_stamp(tmp_path)
    from datetime import datetime, timedelta, timezone

    oldest = datetime.now(timezone.utc) - timedelta(minutes=90)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="3\n", stderr="")
        assert "log" in cmd
        return SimpleNamespace(returncode=0, stdout=oldest.isoformat() + "\n", stderr="")

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    lag = skew.publish_lag(tmp_path, tmp_path)
    assert lag is not None
    assert lag.engine_commits_behind == 3
    assert lag.age_minutes is not None and lag.age_minutes > skew.PUBLISH_LAG_THRESHOLD_MINUTES
    assert len(calls) == 2

    message = skew.publish_lag_message(lag)
    assert message is not None
    assert "3 commit(s)" in message
    assert "percolate-round.py claude-klabauter" in message


def test_publish_lag_below_threshold_minutes_stays_silent(tmp_path, monkeypatch):
    _write_stamp(tmp_path)
    from datetime import datetime, timedelta, timezone

    oldest = datetime.now(timezone.utc) - timedelta(minutes=5)

    def fake_run(cmd, **kwargs):
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=oldest.isoformat() + "\n", stderr="")

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    lag = skew.publish_lag(tmp_path, tmp_path)
    assert lag is not None
    assert lag.engine_commits_behind == 1
    assert skew.publish_lag_message(lag) is None


def test_publish_lag_never_raises_on_unexpected_exception(tmp_path, monkeypatch):
    _write_stamp(tmp_path)

    def fake_run(cmd, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    assert skew.publish_lag(tmp_path, tmp_path) is None


def test_publish_lag_at_most_two_git_calls_total(tmp_path, monkeypatch):
    """Amplification-gate-adjacent bound, pinned locally: never more than
    two subprocess calls for one publish_lag() invocation, matching the
    DR-335 brief's hard constraint."""
    _write_stamp(tmp_path)
    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="2\n", stderr="")
        from datetime import datetime, timezone

        return SimpleNamespace(returncode=0, stdout=datetime.now(timezone.utc).isoformat() + "\n", stderr="")

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    skew.publish_lag(tmp_path, tmp_path)
    assert call_count <= 2
