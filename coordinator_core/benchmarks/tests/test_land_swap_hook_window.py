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


MARKS = {"round_begin": 100.0, "copy_begin": 130.0, "exit_done": 135.0, "publisher_exit": 136.0}


def _dial(started: float, root: str, outcome: str, transport: str = lw.TRANSPORT_HTTP) -> lw.RoundSample:
    return lw.RoundSample(started, 0.01, root, transport, outcome)


def _classify(samples, marks=MARKS, successor=None, scope=lw.SCOPE_WHOLE_LANDING) -> lw.PublishRoundReport:
    return lw.classify_round(
        samples,
        marks,
        hold_scope=scope,
        body_secs=30.0,
        default_root_is_synced=False,
        successor=successor if successor is not None else {"pid": 7, "alive_after_publisher_exit": True},
    )


def test_phase_of_names_the_cut_from_the_drain_slack_to_the_successor_answer() -> None:
    window = lw.cut_window(MARKS, lw.SCOPE_WHOLE_LANDING)
    assert window == (130.0 - lw.CUT_ENTRY_SLACK_SECS, 135.0)
    assert lw.phase_of(99.0, MARKS, window) == "pre_round"
    assert lw.phase_of(110.0, MARKS, window) == "landing"
    assert lw.phase_of(129.5, MARKS, window) == "cut"
    assert lw.phase_of(135.5, MARKS, window) == "post_cut"
    assert lw.phase_of(137.0, MARKS, window) == "post_exit"


def test_a_park_through_the_landing_counts_outside_the_cut_on_both_roots() -> None:
    samples = [_dial(110.0 + i, root, lw.OUTCOME_TIMEOUT) for i in range(5) for root in ("default", "synced")]
    report = _classify(samples)
    assert report.closed_after_delivery_outside_cut == 10
    assert report.closed_after_delivery_inside_cut == 0
    assert report.default_root_parks is True
    assert report.per_root["default"]["kinds_by_phase"] == {"landing": {"timeout": 5}}
    assert not report.within_budget()


def test_failures_confined_to_the_cut_leave_the_budget_met() -> None:
    samples = [
        _dial(110.0, "default", lw.OUTCOME_ANSWERED),
        _dial(110.0, "synced", lw.OUTCOME_ANSWERED),
        _dial(131.0, "synced", lw.OUTCOME_CLOSED),
        _dial(134.0, "synced", lw.OUTCOME_TIMEOUT),
        _dial(137.0, "synced", lw.OUTCOME_ANSWERED),
    ]
    report = _classify(samples)
    assert report.closed_after_delivery_outside_cut == 0
    assert report.closed_after_delivery_inside_cut == 2
    assert report.default_root_parks is False
    assert report.within_budget()


def test_pipe_failures_are_reported_apart_from_the_hook_dial_count() -> None:
    report = _classify([_dial(110.0, "synced", lw.OUTCOME_TIMEOUT, lw.TRANSPORT_PIPE)])
    assert report.closed_after_delivery_outside_cut == 0
    assert report.per_root["_pipe_failures"] == 1


def test_a_successor_that_never_answered_leaves_ancestry_unmeasured() -> None:
    assert _classify([], successor={"pid": None}).ancestry_changes_outcome is None


def test_a_successor_that_dies_with_its_publisher_is_ancestry_causal() -> None:
    dead = _classify([], successor={"pid": 7, "alive_after_publisher_exit": False})
    alive = _classify([_dial(137.0, "synced", lw.OUTCOME_ANSWERED)])
    assert dead.ancestry_changes_outcome is True
    assert alive.ancestry_changes_outcome is False


class _FakePublish:
    def __init__(self, events: list) -> None:
        self._events = events

    def _quiesced_engine_roots(self, roots):
        import contextlib

        @contextlib.contextmanager
        def _hold():
            self._events.append("enter")
            yield
            self._events.append("exit")

        return _hold()


def _run_round(scope: str, monkeypatch, tmp_path: Path) -> list:
    events: list = []
    monkeypatch.setattr(lw, "rewrite_tree", lambda *_a, **_k: events.append("copy"))
    lw.run_modelled_round(
        _FakePublish(events),
        tmp_path,
        hold_scope=scope,
        body_secs=30.0,
        copy_secs=0.0,
        marks={},
        sleep=lambda _secs: events.append("land"),
    )
    return events


def test_the_whole_landing_round_holds_across_the_landing_and_the_copy(monkeypatch, tmp_path: Path) -> None:
    assert _run_round(lw.SCOPE_WHOLE_LANDING, monkeypatch, tmp_path) == ["enter", "land", "copy", "exit"]


def test_the_per_root_round_lands_before_the_hold_and_holds_only_the_copy(monkeypatch, tmp_path: Path) -> None:
    assert _run_round(lw.SCOPE_PER_ROOT, monkeypatch, tmp_path) == ["land", "enter", "copy", "exit"]


def test_hold_scope_reads_the_landing_hold_shape_from_run_round_source() -> None:
    import types

    def whole(_a):
        return [_a, _quiesced_engine_roots([])]  # ), _quiesced_engine_roots(

    def narrowed(_a):
        return _quiesced_engine_roots([_a])

    whole.__doc__ = "x), _quiesced_engine_roots("
    module = types.SimpleNamespace(_run_round=whole)
    assert lw.detect_hold_scope(module) == lw.SCOPE_WHOLE_LANDING
    assert lw.detect_hold_scope(types.SimpleNamespace(_run_round=narrowed)) == lw.SCOPE_PER_ROOT


def test_swap_window_method_check_names_a_called_but_undefined_method(tmp_path: Path) -> None:
    warm = tmp_path / "coordinator_core" / "warm"
    warm.mkdir(parents=True)
    (warm / "server.py").write_text("x = version_state.is_token_skewed(t)\n", encoding="utf-8")
    (warm / "skew.py").write_text("class ServerVersionState:\n    def is_skewed(self): ...\n", encoding="utf-8")
    assert lw.swap_window_method_defined(tmp_path) is False
    (warm / "skew.py").write_text("    def is_token_skewed(self, t): ...\n", encoding="utf-8")
    assert lw.swap_window_method_defined(tmp_path) is True
    (warm / "server.py").write_text("pass\n", encoding="utf-8")
    assert lw.swap_window_method_defined(tmp_path) is None


def test_the_cut_opens_at_hold_begin_so_the_hold_entry_park_is_inside_it() -> None:
    marks = {**MARKS, "hold_begin": 127.0}
    assert lw.cut_window(marks, lw.SCOPE_PER_ROOT) == (127.0, 135.0)
    inside_entry = _classify([_dial(127.5, "synced", lw.OUTCOME_TIMEOUT)], marks=marks, scope=lw.SCOPE_PER_ROOT)
    assert inside_entry.closed_after_delivery_outside_cut == 0
    before_entry = _classify([_dial(126.0, "synced", lw.OUTCOME_TIMEOUT)], marks=marks, scope=lw.SCOPE_PER_ROOT)
    assert before_entry.closed_after_delivery_outside_cut == 1
