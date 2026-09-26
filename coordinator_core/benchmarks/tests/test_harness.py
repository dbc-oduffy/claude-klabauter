
from __future__ import annotations

from unittest import mock

import pytest

from coordinator_core.benchmarks.harness import _collect_samples, _percentile, run

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_percentile_empty_list_raises():
    with pytest.raises(ValueError):
        _percentile([], 50)


def test_percentile_single_sample_returns_that_sample():
    assert _percentile([42.0], 50) == 42.0
    assert _percentile([42.0], 99) == 42.0


def test_percentile_adversarial_n5_distinguishes_correct_from_degenerate():
    sorted_samples = [10.0, 20.0, 30.0, 40.0, 100.0]

    p50 = _percentile(sorted_samples, 50)
    p95 = _percentile(sorted_samples, 95)
    p99 = _percentile(sorted_samples, 99)

    assert p50 == 30.0, f"degenerate/broken percentile: p50={p50!r} (expected 30.0)"
    assert p50 < p95 <= p99
    assert p99 <= sorted_samples[-1]
    assert p50 != sorted_samples[-1]


def test_percentile_monotonic_across_pcts():
    sorted_samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    p50 = _percentile(sorted_samples, 50)
    p95 = _percentile(sorted_samples, 95)
    p99 = _percentile(sorted_samples, 99)
    assert sorted_samples[0] <= p50 <= p95 <= p99 <= sorted_samples[-1]


@mock.patch("coordinator_core.benchmarks.harness.time_invocation")
def test_collect_samples_discards_warmup_runs(mock_time_invocation):
    """Asserts the warmup-discard contract precisely: exactly `warmup` extra
    calls happen (discarded), and the RETURNED sample list has length `n`
    only -- warmup results never leak into the returned samples.
    """
    mock_time_invocation.side_effect = [float(i) for i in range(10)]

    warmup = 2
    n = 3
    result = _collect_samples("ping", "{}", None, n, warmup)

    assert mock_time_invocation.call_count == warmup + n
    assert len(result) == n
    assert result == [2.0, 3.0, 4.0]


@mock.patch("coordinator_core.benchmarks.harness.time_invocation")
def test_collect_samples_zero_warmup(mock_time_invocation):
    mock_time_invocation.side_effect = [10.0, 20.0]

    result = _collect_samples("ping", "{}", None, n=2, warmup=0)

    assert mock_time_invocation.call_count == 2
    assert result == [10.0, 20.0]


def test_run_rejects_n_below_one_before_any_subprocess_spawn():
    with pytest.raises(ValueError, match="n must be >= 1"):
        run(ops=["ping"], n=0)


def test_run_rejects_negative_warmup_before_any_subprocess_spawn():
    with pytest.raises(ValueError, match="warmup must be >= 0"):
        run(ops=["ping"], n=1, warmup=-1)
