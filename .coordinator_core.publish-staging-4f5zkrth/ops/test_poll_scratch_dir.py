"""Tests for coordinator_core.ops.poll_scratch_dir (op fanout.poll_scratch_dir).

Wave-3 settlement B6 coverage: monotonic deadline loop, count-reached and timeout
verdicts, missing-scratch_dir structured error (never a zero count), param
validation, and the CC-4 double-invocation idempotency proof.
"""
from __future__ import annotations

import pytest

from coordinator_core.ops import poll_scratch_dir
from coordinator_core.ops.poll_scratch_dir import _poll_scratch_dir


def _params(scratch_dir, min_count=1, timeout_seconds=0.2, poll_interval_seconds=0.01):
    return {
        "scratch_dir": str(scratch_dir),
        "min_count": min_count,
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
    }


def test_count_already_reached_returns_immediately(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "a.json").write_text("{}", encoding="utf-8")
    (scratch / "b.json").write_text("{}", encoding="utf-8")

    result = _poll_scratch_dir(_params(scratch, min_count=2))

    assert result["status"] == "count_reached"
    assert result["count"] == 2
    assert result["elapsed_seconds"] >= 0.0


def test_min_count_zero_on_empty_dir(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _poll_scratch_dir(_params(scratch, min_count=0))

    assert result["status"] == "count_reached"
    assert result["count"] == 0


def test_timeout_reports_last_count(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "only.json").write_text("{}", encoding="utf-8")

    result = _poll_scratch_dir(
        _params(scratch, min_count=5, timeout_seconds=0.05, poll_interval_seconds=0.01)
    )

    assert result["status"] == "timeout"
    assert result["count"] == 1
    assert result["elapsed_seconds"] >= 0.05


def test_missing_scratch_dir_structured_error_not_zero_count(tmp_path):
    ghost = tmp_path / "never-created"

    result = _poll_scratch_dir(_params(ghost))

    assert set(result.keys()) == {"error"}
    assert "does not exist" in result["error"]


def test_scratch_dir_is_a_file_structured_error(tmp_path):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")

    result = _poll_scratch_dir(_params(not_a_dir))

    assert set(result.keys()) == {"error"}


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"scratch_dir": ""},
        {"scratch_dir": "s", "min_count": -1, "timeout_seconds": 1, "poll_interval_seconds": 0.1},
        {"scratch_dir": "s", "min_count": True, "timeout_seconds": 1, "poll_interval_seconds": 0.1},
        {"scratch_dir": "s", "min_count": 1, "timeout_seconds": -1, "poll_interval_seconds": 0.1},
        {"scratch_dir": "s", "min_count": 1, "timeout_seconds": 1, "poll_interval_seconds": 0},
        {"scratch_dir": "s", "min_count": 1, "timeout_seconds": 1},
    ],
)
def test_invalid_params_structured_error(bad):
    assert "error" in _poll_scratch_dir(bad)


def test_double_invocation_identical_verdict(tmp_path):
    """CC-4 idempotency proof: read-only — the second call with identical inputs
    re-polls the same on-disk state and returns the same verdict/count (elapsed
    is a measurement, compared only for validity)."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "r1.json").write_text("{}", encoding="utf-8")

    params = _params(scratch, min_count=1)
    first = _poll_scratch_dir(params)
    second = _poll_scratch_dir(params)

    assert first["status"] == second["status"] == "count_reached"
    assert first["count"] == second["count"] == 1
    assert second["elapsed_seconds"] >= 0.0


def test_registered_under_op_key():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("fanout.poll_scratch_dir") is _poll_scratch_dir


# ---------------------------------------------------------------------------
# Timeout ceiling — a caller may ask for LESS wait, never for more
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock whose only advance is the sleeps the op itself asks for.

    Nothing here waits in wall-clock time, so the loop's own arithmetic — not a
    real timer — decides when the op returns, and an unclamped budget shows up
    as a fake-clock reading in the thousands rather than as a slow test.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(poll_scratch_dir.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(poll_scratch_dir.time, "sleep", clock.sleep)
    return clock


def test_over_ceiling_timeout_is_clamped(tmp_path, fake_clock):
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _poll_scratch_dir(
        _params(scratch, min_count=1, timeout_seconds=86400.0, poll_interval_seconds=1.0)
    )

    assert result["status"] == "timeout"
    assert result["elapsed_seconds"] == poll_scratch_dir.MAX_TIMEOUT_SECONDS, (
        "a caller-supplied timeout_seconds above MAX_TIMEOUT_SECONDS must be "
        "clamped, and elapsed_seconds must report the budget actually spent — "
        "never the one the caller asked for"
    )


def test_over_ceiling_poll_interval_is_clamped(tmp_path, fake_clock):
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    _poll_scratch_dir(
        _params(scratch, min_count=1, timeout_seconds=600.0, poll_interval_seconds=99999.0)
    )

    assert fake_clock.sleep_calls, "the op must have polled at least once"
    assert max(fake_clock.sleep_calls) <= poll_scratch_dir.MAX_POLL_INTERVAL_SECONDS


def test_sleep_never_overshoots_the_deadline(tmp_path, fake_clock):
    """The ceiling only bounds the block if the last sleep is clamped too — a
    poll interval longer than the remaining budget would otherwise park the
    dispatch worker well past the timeout the caller was told bounds it."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _poll_scratch_dir(
        _params(scratch, min_count=1, timeout_seconds=5.0, poll_interval_seconds=30.0)
    )

    assert result["status"] == "timeout"
    assert fake_clock.now == 5.0
    assert fake_clock.sleep_calls == [5.0]


def test_under_ceiling_timeout_is_honoured(tmp_path, fake_clock):
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    result = _poll_scratch_dir(
        _params(scratch, min_count=1, timeout_seconds=3.0, poll_interval_seconds=1.0)
    )

    assert result["status"] == "timeout"
    assert result["elapsed_seconds"] == 3.0
