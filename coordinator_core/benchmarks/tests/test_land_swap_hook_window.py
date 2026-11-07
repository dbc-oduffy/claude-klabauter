"""Tests for the land-swap hook-window harness: the reducer, the modelled hook request, and a
short end-to-end swap on both respawn orders."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks import land_swap_hook_window as lw
from coordinator_core.warm import breadcrumb

import pytest

pytestmark = pytest.mark.complete

BUDGET = breadcrumb.DRAIN_WAIT_SECS


def _fast_stop(_root: Path) -> None:
    return None


def test_summarize_reports_the_longest_unanswered_run() -> None:
    samples = [
        lw.Sample(0.0, 0.01, True),
        lw.Sample(0.1, 0.02, False),
        lw.Sample(0.2, 2.13, True),
        lw.Sample(2.4, 0.01, True),
        lw.Sample(2.5, 0.01, False),
        lw.Sample(2.6, 0.01, True),
    ]
    report = lw.summarize(samples)
    assert report.max_unanswered_secs == 2.23
    assert report.slow_answers == 1
    assert report.requests == 6


def test_summarize_all_answered_in_budget_is_zero() -> None:
    samples = [lw.Sample(i * 0.1, 0.01, True) for i in range(5)]
    report = lw.summarize(samples)
    assert report.max_unanswered_secs == 0.0
    assert report.slow_answers == 0
    assert report.within_budget()


def test_summarize_open_run_counts_to_the_last_sample() -> None:
    report = lw.summarize([lw.Sample(0.0, 0.01, True), lw.Sample(0.1, 0.01, False), lw.Sample(0.2, 0.01, False)])
    assert report.max_unanswered_secs == 0.11


def test_hook_request_with_the_pair_up_is_answered_immediately(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    pair = lw.FakePair(boot_secs=10)
    pair.start_answering()
    assert lw.hook_request(pair, tmp_path, lw.HOOK_PATH_BASE) is True


def test_hook_request_under_a_held_marker_refuses_at_base_and_degrades_when_bounded(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    pair = lw.FakePair(boot_secs=10)
    with breadcrumb.hold_publish_marker(tmp_path, holder="test"):
        assert lw.hook_request(pair, tmp_path, lw.HOOK_PATH_BASE) is False
        assert lw.hook_request(pair, tmp_path, lw.HOOK_PATH_BOUNDED) is True
    assert not pair.answering
    pair.stop()


def test_base_swap_leaves_a_window_spanning_copy_and_boot() -> None:
    report = lw.run_swap(
        copy_secs=1.0,
        boot_secs=1.0,
        pre_roll_secs=0.1,
        tail_secs=0.1,
        stop_pair=_fast_stop,
    )
    assert report.max_unanswered_secs >= 1.5
    assert report.slow_answers >= 1
    assert not report.within_budget()


def test_pre_release_respawn_with_bounded_hooks_leaves_no_window() -> None:
    report = lw.run_swap(
        copy_secs=0.4,
        boot_secs=0.6,
        respawn=lw.RESPAWN_PRE_RELEASE,
        hook_path=lw.HOOK_PATH_BOUNDED,
        pre_roll_secs=0.1,
        tail_secs=0.1,
        stop_pair=_fast_stop,
    )
    assert report.max_unanswered_secs <= BUDGET
    assert report.slow_answers == 0


def test_pre_release_respawn_at_base_hook_path_still_has_a_window() -> None:
    report = lw.run_swap(
        copy_secs=1.0,
        boot_secs=1.0,
        respawn=lw.RESPAWN_PRE_RELEASE,
        hook_path=lw.HOOK_PATH_BASE,
        pre_roll_secs=0.1,
        tail_secs=0.1,
        stop_pair=_fast_stop,
    )
    assert report.max_unanswered_secs >= 1.5


def test_run_swap_restores_the_runtime_base_env(monkeypatch) -> None:
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, "sentinel")
    lw.run_swap(copy_secs=0.1, boot_secs=0.1, pre_roll_secs=0.05, tail_secs=0.05, stop_pair=_fast_stop)
    import os

    assert os.environ[breadcrumb.RUNTIME_BASE_ENV] == "sentinel"


def test_main_assert_budget_fails_the_base_swap(capsys, monkeypatch) -> None:
    from coordinator_core.telemetry import op_latency

    monkeypatch.setenv(op_latency.ORIGIN_ENV, op_latency.BENCHMARK)
    code = lw.main(["--copy-secs", "1.0", "--boot-secs", "1.0", "--assert-budget"])
    out = capsys.readouterr().out
    assert code == 1
    assert "max_unanswered_secs=" in out and "slow_answers=" in out
