"""Unit tests for coordinator_core.benchmarks.harness's `_percentile` and
`_collect_samples` helpers.

Review: code-reviewer (Slice C F6, P2) -- these two helpers previously had no
isolated unit coverage; they were proven ONLY transitively by
test_integration.py's real-subprocess run at N=3, which (per the same
review's Finding 5) cannot adversarially distinguish a correct percentile
implementation from a degenerate one (at N=3, p50/p95/p99 frequently collapse
to the same interpolated value, so a broken implementation that just returns
`sorted_samples[-1]` for everything above p50 would still satisfy a bare
`min <= p50 <= p95 <= p99` monotonicity check). This file closes that gap
with an N>=5 adversarial case plus a warmup-discard call-count/return-length
assertion, both fully mocked -- no real subprocess spawning.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C6.
"""

from __future__ import annotations

from unittest import mock

import pytest

from coordinator_core.benchmarks.harness import _collect_samples, _percentile, run

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
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
    """N=5 sorted samples where a degenerate implementation that always
    returns sorted_samples[-1] for any percentile >= 50 is distinguishable
    from the real nearest-rank-with-interpolation implementation.

    Samples: [10, 20, 30, 40, 100] (index 0..4). A "return sorted[-1] for
    p>=50" degenerate implementation would report p50 == p95 == p99 == 100.
    The real implementation must NOT collapse p50 to the max value -- it
    must land strictly below the top sample given this spread.
    """
    sorted_samples = [10.0, 20.0, 30.0, 40.0, 100.0]

    p50 = _percentile(sorted_samples, 50)
    p95 = _percentile(sorted_samples, 95)
    p99 = _percentile(sorted_samples, 99)

    # Real nearest-rank-with-interpolation semantics for N=5:
    # rank = pct/100 * (N-1); p50 -> rank=2.0 -> sorted[2] == 30.0 exactly.
    assert p50 == 30.0, f"degenerate/broken percentile: p50={p50!r} (expected 30.0)"
    assert p50 < p95 <= p99
    assert p99 <= sorted_samples[-1]
    # The distinguishing assertion: a "return max for p>=50" degenerate
    # implementation would report p50 == 100.0, which this rejects.
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
    # Distinct return values per call let us prove which calls' results
    # ended up in the returned list.
    mock_time_invocation.side_effect = [float(i) for i in range(10)]

    warmup = 2
    n = 3
    result = _collect_samples("ping", "{}", None, n, warmup)

    assert mock_time_invocation.call_count == warmup + n
    assert len(result) == n
    # The first `warmup` calls' return values (0.0, 1.0) must NOT appear in
    # the returned samples; only the last `n` calls' values (2.0, 3.0, 4.0)
    # should be present.
    assert result == [2.0, 3.0, 4.0]


@mock.patch("coordinator_core.benchmarks.harness.time_invocation")
def test_collect_samples_zero_warmup(mock_time_invocation):
    mock_time_invocation.side_effect = [10.0, 20.0]

    result = _collect_samples("ping", "{}", None, n=2, warmup=0)

    assert mock_time_invocation.call_count == 2
    assert result == [10.0, 20.0]


def test_run_rejects_n_below_one_before_any_subprocess_spawn():
    """Review: code-reviewer (Slice B F2, P2) -- `--n 0` must fail loud with
    a clear message naming the bad param, not an opaque IndexError deep in
    the sample-collection loop. Asserted here without mocking subprocess --
    the ValueError must fire before harness.run() gets anywhere near
    _capture_code_sha()/floor measurement/subprocess spawning.
    """
    with pytest.raises(ValueError, match="n must be >= 1"):
        run(ops=["ping"], n=0)


def test_run_rejects_negative_warmup_before_any_subprocess_spawn():
    with pytest.raises(ValueError, match="warmup must be >= 0"):
        run(ops=["ping"], n=1, warmup=-1)
