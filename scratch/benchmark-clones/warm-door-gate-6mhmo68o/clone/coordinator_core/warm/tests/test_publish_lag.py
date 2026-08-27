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


def test_publish_lag_absorbs_an_ordinary_spawn_oserror(tmp_path, monkeypatch):
    """Renamed from `..._never_raises_on_unexpected_exception`: `OSError`
    ("git not found") is the ordinary, expected spawn-failure shape, not
    the unexpected one -- and it is caught by the same bare
    `except Exception` this module's outer handler now uses regardless of
    type. `test_publish_lag_absorbs_a_non_oserror_exception` below is the
    test that actually exercises the "unexpected" claim (a `ValueError`,
    previously uncaught). Kept as a separate case rather than folded in:
    a real git-not-found spawn failure and a corrupt-stamp `ValueError`
    are different scenarios worth pinning independently, even though both
    now resolve through the same except clause.
    """
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


# ---------------------------------------------------------------------------
# Regression: the oldest-commit call must not use `--reverse ... -1`
# ---------------------------------------------------------------------------


def test_publish_lag_takes_the_oldest_unpublished_commit_not_the_newest(tmp_path, monkeypatch):
    """git applies a commit limit BEFORE `--reverse`, so `--reverse -1` yields
    the NEWEST commit. That pins age_minutes near zero on an active branch and
    silently holds the advisory below its threshold forever -- the signal is
    disabled while every field still looks populated. Caught live: 97 commits
    unpublished reported as 0.4 minutes old.
    """
    _write_stamp(tmp_path, "stamped")
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout="3\n")
        # newest first, exactly as `git log` orders it
        return SimpleNamespace(
            returncode=0,
            stdout="2026-08-19T22:00:00+01:00\n"
                   "2026-08-19T21:00:00+01:00\n"
                   "2026-08-19T19:41:03+01:00\n",
        )

    monkeypatch.setattr(skew.subprocess, "run", fake_run)
    lag = skew.publish_lag(tmp_path, tmp_path)

    assert lag is not None
    assert lag.oldest_unpublished_iso == "2026-08-19T19:41:03+01:00"
    assert lag.age_minutes > 60

    log_cmd = next(c for c in seen if "log" in c)
    assert "--reverse" not in log_cmd, "`--reverse` with a limit returns the newest commit"
    assert "-1" not in log_cmd, "a commit limit here silently selects the newest commit"
    assert len(seen) == 2, "at most two bounded git calls (amplification gate)"


def test_publish_lag_absorbs_a_non_oserror_exception(tmp_path, monkeypatch):
    """The docstring promises "any unexpected exception" returns None. The
    original test injected OSError -- a type already caught by name -- so it
    passed without covering the claim. A ValueError is the honest probe, and
    UnicodeDecodeError (a ValueError subclass) is the real-world case: a
    stamp file carrying invalid UTF-8.
    """
    _write_stamp(tmp_path, "stamped")

    def boom(*a, **k):
        raise ValueError("not an OSError")

    monkeypatch.setattr(skew.subprocess, "run", boom)
    assert skew.publish_lag(tmp_path, tmp_path) is None


def test_read_engine_stamp_sha_absorbs_invalid_utf8(tmp_path):
    stamp = tmp_path / "coordinator_core" / skew.ENGINE_STAMP_FILENAME
    stamp.parent.mkdir(parents=True)
    stamp.write_bytes(b"sha:\xff\xfe not utf-8\n")
    assert skew.read_engine_stamp_sha(tmp_path) is None


def test_publish_lag_message_scope_sentence_differs_by_site():
    """The shared sentence was false at close-out: nothing is executing
    there. Both sites must still carry one fact and one runnable
    alternative (guard-messaging.md § Register).
    """
    lag = skew.PublishLag(
        stamp_sha="abc",
        engine_commits_behind=5,
        oldest_unpublished_iso="2026-08-19T19:41:03+01:00",
        age_minutes=180.0,
    )
    fire = skew.publish_lag_message(lag, site="fire")
    close = skew.publish_lag_message(lag, site="close-out")
    assert "This run executes" in fire
    assert "This run executes" not in close
    for msg in (fire, close):
        assert "5 commit(s)" in msg
        assert "percolate-round.py claude-klabauter" in msg
