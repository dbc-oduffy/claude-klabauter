"""Tests for coordinator_core.ops.poll_scratch_dir (op fanout.poll_scratch_dir).

Wave-3 settlement B6 coverage: monotonic deadline loop, count-reached and timeout
verdicts, missing-scratch_dir structured error (never a zero count), param
validation, and the CC-4 double-invocation idempotency proof.
"""
from __future__ import annotations

import pytest

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
