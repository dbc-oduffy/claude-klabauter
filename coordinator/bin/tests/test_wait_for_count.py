"""test_wait_for_count.py — pytest tests for coordinator/bin/wait-for-count.py.

Covers count_matches (non-existent dir counts as 0, pattern filtering),
wait_for_count's polling contract (returns immediately when already met, no
sleep call in that case; times out and returns the last-seen count when the
deadline elapses; polls again after a sleep when the count changes
mid-wait), and the CLI's exit-code/message contract (0 + stdout summary on
met, 1 + stderr TIMEOUT line otherwise).

wait_for_count's now_fn/sleep_fn are injected fakes throughout -- no test in
this suite performs a real time.sleep, so the whole file runs in
well under a second.

Run: python -m pytest coordinator/bin/tests/test_wait_for_count.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("wait_for_count", _BIN_DIR / "wait-for-count.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class _FakeClock:
    """A monotonic clock fake: now_fn returns the current tick; sleep_fn
    advances the tick by the requested amount (no real wall-clock wait) and
    records each call for assertion."""

    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleep_calls: list[float] = []

    def now_fn(self) -> float:
        return self.now

    def sleep_fn(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------------------
# count_matches
# ---------------------------------------------------------------------------


def test_count_matches_nonexistent_dir_is_zero(mod, tmp_path):
    missing = tmp_path / "does-not-exist"
    assert mod.count_matches(missing, "*") == 0


def test_count_matches_counts_all_entries_by_default(mod, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    (tmp_path / "subdir").mkdir()

    assert mod.count_matches(tmp_path, "*") == 3


def test_count_matches_respects_pattern(mod, tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    (tmp_path / "c.txt").write_text("x")

    assert mod.count_matches(tmp_path, "*.json") == 2


# ---------------------------------------------------------------------------
# wait_for_count
# ---------------------------------------------------------------------------


def test_wait_for_count_returns_immediately_when_already_met(mod, tmp_path):
    (tmp_path / "a").write_text("x")
    (tmp_path / "b").write_text("x")
    clock = _FakeClock()

    met, count = mod.wait_for_count(
        tmp_path, "*", 2, timeout_sec=600, poll_interval_sec=30,
        now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
    )

    assert met is True
    assert count == 2
    assert clock.sleep_calls == []


def test_wait_for_count_polls_until_threshold_met(mod, tmp_path):
    clock = _FakeClock()
    ticks = {"n": 0}

    def sleep_and_populate(seconds: float) -> None:
        clock.sleep_fn(seconds)
        ticks["n"] += 1
        if ticks["n"] == 2:
            (tmp_path / "landed").write_text("x")

    met, count = mod.wait_for_count(
        tmp_path, "*", 1, timeout_sec=600, poll_interval_sec=30,
        now_fn=clock.now_fn, sleep_fn=sleep_and_populate,
    )

    assert met is True
    assert count == 1
    assert ticks["n"] == 2


def test_wait_for_count_times_out_with_last_seen_count(mod, tmp_path):
    (tmp_path / "only-one").write_text("x")
    clock = _FakeClock()

    met, count = mod.wait_for_count(
        tmp_path, "*", 5, timeout_sec=100, poll_interval_sec=30,
        now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
    )

    assert met is False
    assert count == 1
    assert clock.now >= 100


def test_wait_for_count_clamps_final_sleep_to_remaining_time(mod, tmp_path):
    clock = _FakeClock()

    met, count = mod.wait_for_count(
        tmp_path, "*", 1, timeout_sec=10, poll_interval_sec=30,
        now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
    )

    assert met is False
    # poll_interval_sec (30) exceeds timeout_sec (10) -- the single sleep
    # call must be clamped to the remaining budget, not the full interval.
    assert clock.sleep_calls == [10]


def test_wait_for_count_zero_timeout_never_sleeps(mod, tmp_path):
    clock = _FakeClock()

    met, count = mod.wait_for_count(
        tmp_path, "*", 1, timeout_sec=0, poll_interval_sec=30,
        now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
    )

    assert met is False
    assert clock.sleep_calls == []


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


def _run_cli(mod, args: list[str]):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(args)
    return rc, out.getvalue(), err.getvalue()


def test_cli_exits_zero_and_prints_summary_when_met(mod, tmp_path):
    (tmp_path / "x").write_text("1")

    rc, out, err = _run_cli(
        mod, ["--dir", str(tmp_path), "--min", "1", "--timeout-sec", "0"]
    )

    assert rc == 0
    assert f"count=1 threshold=1 dir={tmp_path}" in out
    assert err == ""


def test_cli_exits_one_and_prints_timeout_to_stderr(mod, tmp_path):
    rc, out, err = _run_cli(
        mod, ["--dir", str(tmp_path), "--min", "3", "--timeout-sec", "0"]
    )

    assert rc == 1
    assert out == ""
    assert "TIMEOUT" in err
    assert "threshold=3" in err


def test_cli_treats_missing_dir_as_zero_not_error(mod, tmp_path):
    missing = tmp_path / "not-yet-created"

    rc, out, err = _run_cli(
        mod, ["--dir", str(missing), "--min", "1", "--timeout-sec", "0"]
    )

    assert rc == 1
    assert "count=0" in err


# ---------------------------------------------------------------------------
# Timeout ceiling — a caller may ask for LESS, never for more
# ---------------------------------------------------------------------------


def test_over_ceiling_dials_are_clamped(mod):
    over = mod.clamp_dials(mod.MAX_TIMEOUT_SEC * 100, mod.MAX_POLL_INTERVAL_SEC * 100)
    assert over == (mod.MAX_TIMEOUT_SEC, mod.MAX_POLL_INTERVAL_SEC)


def test_under_ceiling_dials_pass_through(mod):
    assert mod.clamp_dials(5.0, 1.0) == (5.0, 1.0)


def test_clamp_dials_is_idempotent(mod):
    once = mod.clamp_dials(mod.MAX_TIMEOUT_SEC * 100, mod.MAX_POLL_INTERVAL_SEC * 100)
    assert mod.clamp_dials(*once) == once


def test_wait_for_count_cannot_be_asked_to_wait_past_the_ceiling(mod, tmp_path):
    """A caller naming a day-long budget must still return at the ceiling.

    The fake clock advances only on sleep, so the loop's own arithmetic — not
    wall time — decides when this returns. An unclamped timeout_sec would drive
    the fake clock to 86400, and an unclamped poll_interval_sec would record a
    single 86400s sleep call.
    """
    clock = _FakeClock()

    met, count = mod.wait_for_count(
        tmp_path, "*", 1, timeout_sec=86400, poll_interval_sec=86400,
        now_fn=clock.now_fn, sleep_fn=clock.sleep_fn,
    )

    assert met is False
    assert count == 0
    assert clock.now == mod.MAX_TIMEOUT_SEC
    assert max(clock.sleep_calls) <= mod.MAX_POLL_INTERVAL_SEC


def test_cli_timeout_message_reports_the_clamped_budget(mod, tmp_path):
    """The TIMEOUT line must name what was actually waited, not what was asked
    for — reporting the over-ask back would tell the caller its dial worked."""
    rc, out, err = _run_cli(
        mod,
        ["--dir", str(tmp_path), "--min", "1", "--timeout-sec", "0", "--poll-interval-sec", "99999"],
    )

    assert rc == 1
    assert "99999" not in err
