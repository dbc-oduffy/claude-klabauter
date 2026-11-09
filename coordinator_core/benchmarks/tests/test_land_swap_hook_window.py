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
    code = lw.main(["--arm", "model", "--copy-secs", "1.0", "--boot-secs", "1.0", "--assert-budget"])
    out = capsys.readouterr().out
    assert code == 1
    assert "max_unanswered_secs=" in out and "slow_answers=" in out
    assert "unevaluated_guard_count=" in out


def test_summarize_counts_every_guard_that_got_no_evaluated_answer() -> None:
    samples = [
        lw.Sample(0.0, 0.01, True),
        lw.Sample(0.1, 0.02, False),
        lw.Sample(0.2, 2.13, True),
        lw.Sample(2.4, 0.01, True),
    ]
    report = lw.summarize(samples)
    assert report.unevaluated_guard_count == 2


def test_an_unevaluated_guard_alone_breaks_the_budget() -> None:
    clean = lw.summarize([lw.Sample(0.0, 0.01, True)])
    assert clean.unevaluated_guard_count == 0 and clean.within_budget()
    failed = lw.WindowReport(
        max_unanswered_secs=0.0,
        slow_answers=0,
        requests=1,
        respawn="",
        hook_path="",
        unevaluated_guard_count=1,
    )
    assert not failed.within_budget()


def test_the_unanswered_budget_is_the_half_second_brightline() -> None:
    over = lw.WindowReport(
        max_unanswered_secs=lw.UNANSWERED_BUDGET_SECS + 0.1,
        slow_answers=0,
        requests=1,
        respawn="",
        hook_path="",
    )
    assert lw.UNANSWERED_BUDGET_SECS == 0.5
    assert not over.within_budget()


def _engine_root(tmp_path: Path) -> Path:
    pkg = tmp_path / "engine" / "coordinator_core"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / lw.skew.ENGINE_STAMP_FILENAME).write_text("sha:old\n", encoding="utf-8")
    return tmp_path / "engine"


def test_rewrite_tree_replaces_files_without_writing_through_a_hard_link(tmp_path: Path) -> None:
    import os

    root = _engine_root(tmp_path)
    original = tmp_path / "checkout.py"
    original.write_text("x = 1\n", encoding="utf-8")
    linked = root / "coordinator_core" / "linked.py"
    os.link(original, linked)
    old_token = lw.skew.compute_client_token(root)

    count = lw.rewrite_tree(root, 0.0, new_stamp="sha:new\n")

    assert count == 2
    assert original.read_text(encoding="utf-8") == "x = 1\n"
    assert os.stat(original).st_nlink == 1
    assert lw.skew.compute_client_token(root) != old_token
    assert not list((root / "coordinator_core").glob("*.swap"))


def test_rewrite_tree_takes_at_least_the_copy_time(tmp_path: Path) -> None:
    import time

    root = _engine_root(tmp_path)
    began = time.monotonic()
    lw.rewrite_tree(root, 0.3)
    assert time.monotonic() - began >= 0.3


def test_endpoint_follows_the_token_and_the_engine_root(tmp_path: Path) -> None:
    root = _engine_root(tmp_path)
    one = lw.endpoint_for(root, "tokenone", sid="S-1")
    other = lw.endpoint_for(root, "tokentwo", sid="S-1")
    assert one != other
    assert one.endswith("tokenone") or one.endswith("tokenone.sock")
    assert lw.endpoint_for(_engine_root(tmp_path / "second"), "tokenone", sid="S-1") != one


def test_reply_classifiers_tell_an_answer_from_a_skew_refusal_and_silence() -> None:
    answer = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    skew_reply = {"jsonrpc": "2.0", "id": 1, "error": {"code": lw.ENGINE_SKEW_CODE, "message": "stale"}}
    other_error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}}
    assert lw._is_answer(answer) and not lw._is_skew(answer)
    assert lw._is_skew(skew_reply) and not lw._is_answer(skew_reply)
    assert not lw._is_answer(other_error) and not lw._is_skew(other_error)
    assert not lw._is_answer(None) and not lw._is_skew(None)


def _spike(**overrides) -> lw.SpikeReport:
    base = dict(
        baseline_ok=3,
        replaced_files=10,
        sequential_ok=30,
        sequential_total=30,
        skew_refusals=0,
        first_skew_after_secs=None,
        concurrent_ok=12,
        concurrent_total=12,
        server_alive_after_probes=True,
        failure_samples=(),
        reachable_after_stamp_flip=True,
    )
    base.update(overrides)
    return lw.SpikeReport(**base)


def test_spike_verdict_is_allowed_only_when_nothing_failed() -> None:
    assert _spike().verdict == "allowed"


def test_spike_verdict_refuses_each_way_the_old_pair_can_stop_answering() -> None:
    assert _spike(skew_refusals=1, first_skew_after_secs=0.3).verdict == "not-allowed"
    assert _spike(sequential_ok=1).verdict == "not-allowed"
    assert _spike(concurrent_ok=0).verdict == "not-allowed"
    assert _spike(server_alive_after_probes=False).verdict == "not-allowed"
    assert _spike(reachable_after_stamp_flip=False).verdict == "not-allowed"


def test_production_handover_runs_the_publish_order_with_the_marker_held_around_the_successor(
    tmp_path: Path, monkeypatch
) -> None:
    from coordinator_core.percolate import round as round_mod

    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = _engine_root(tmp_path)
    marker_dir = breadcrumb.svc_dir(root)
    events: list[str] = []

    def _stop(_root, **_k):
        events.append(f"stop held={breadcrumb.publish_marker_held(marker_dir)}")

    def _start(_root, *, err, ready_budget_secs, old_stamp):
        events.append(f"start held={breadcrumb.publish_marker_held(marker_dir)} pinned={old_stamp is not None}")
        return True

    monkeypatch.setattr(lw.pair_stop, "stop_warm_pair", _stop)
    monkeypatch.setattr(round_mod, "start_successor_pair_in_hold", _start)
    marks: dict = {}

    answered = lw.production_handover(root, copy_secs=0.0, marks=marks)

    assert answered is True
    assert events == ["start held=True pinned=True"], "the old pair is stopped only by the successor start"
    assert not breadcrumb.publish_marker_held(marker_dir)
    assert list(marks) == ["copy_done", "successor_returned", "released"]
    assert marks["released"] >= marks["successor_returned"] >= marks["copy_done"]
