"""Tests for the continuous-caller swap harness: the reducer, the phase wrappers, the client
re-aim, the caller loop, and `run`/`main` wired against stand-ins for the real pair."""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_swap_hook_window as lw
from coordinator_core.benchmarks import swap_continuous_caller as scc
from coordinator_core.warm import breadcrumb, client, pair_stop, skew

pytestmark = pytest.mark.complete


def _req(send: float, rtt: float | None, process_ms: float = 1.0, token: str = "t") -> scc.Request:
    return scc.Request(send, None if rtt is None else send + rtt, process_ms, token)


def _engine_root(tmp_path: Path) -> Path:
    pkg = tmp_path / "engine" / "coordinator_core"
    pkg.mkdir(parents=True)
    (pkg / skew.ENGINE_STAMP_FILENAME).write_text("sha:old\n", encoding="utf-8")
    return tmp_path / "engine"


def test_report_reduces_gap_between_answers_rtt_and_process_time() -> None:
    report = scc.SwapReport(
        requests=(
            _req(100.0, 0.010, process_ms=2.0),
            _req(100.1, 0.030, process_ms=9.0),
            _req(100.2, None),
            _req(101.4, 0.020, process_ms=3.0),
        )
    )
    assert report.no_response == 1
    assert report.max_gap_ms == pytest.approx(1290.0, abs=0.2)  # 100.13 -> 101.42
    assert report.max_process_ms == 9.0
    assert report.p50_ms == 20.0


def test_budget_holds_only_when_all_three_bars_hold() -> None:
    clean = [_req(i * 0.1, 0.01) for i in range(5)]
    assert scc.SwapReport(requests=tuple(clean)).within_budget()
    assert not scc.SwapReport(requests=(*clean, _req(1.0, None))).within_budget()
    assert not scc.SwapReport(requests=(*clean, _req(1.5, 0.01))).within_budget()  # 1s gap
    assert not scc.SwapReport(requests=(*clean, _req(0.5, 0.01, process_ms=250.0))).within_budget()


def test_an_empty_run_reports_zeros_not_a_pass_by_crash() -> None:
    report = scc.SwapReport(requests=())
    assert (report.max_gap_ms, report.no_response, report.max_process_ms, report.p50_ms) == (0.0, 0, 0.0, 0.0)


def test_falsifier_line_names_the_four_numbers_and_the_timeline_one_line_per_phase() -> None:
    phases = {name: 50.0 + i * 0.5 for i, name in enumerate(scc.PHASES)}
    phases["old_pair_stop_done"] = 53.0
    report = scc.SwapReport(requests=(_req(50.0, 0.01),), phases=phases, successor_answered_before_release=True)
    line = report.falsifier_line()
    for key in ("max_gap_ms=", "no_response=", "max_process_ms=", "p50_ms="):
        assert key in line
    timeline = report.timeline_lines()
    assert [name in text for name, text in zip(scc.PHASES, timeline)] == [True] * len(scc.PHASES)
    assert "done +" in timeline[scc.PHASES.index("old_pair_stop")]


def test_a_phase_never_reached_is_said_so() -> None:
    report = scc.SwapReport(requests=(), phases={"marker_take": 1.0})
    assert sum("not reached" in line for line in report.timeline_lines()) == len(scc.PHASES) - 1


def test_phase_clock_stamps_the_swap_in_order_and_restores_every_primitive(monkeypatch, tmp_path: Path) -> None:
    @contextlib.contextmanager
    def marker(_root, *, holder):
        yield tmp_path

    writes: list = []
    monkeypatch.setattr(breadcrumb, "hold_publish_marker", marker)
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **k: True)
    monkeypatch.setattr(breadcrumb, "wait_for_successor_answer", lambda *a, **k: {"pid": 1})
    monkeypatch.setattr(skew, "replace_engine_stamp_bytes", lambda root, data: writes.append(data))
    monkeypatch.setattr(pair_stop, "stop_warm_pair", lambda *a, **k: "stopped")
    originals = (
        breadcrumb.hold_publish_marker,
        client.spawn_detached,
        breadcrumb.wait_for_successor_answer,
        skew.replace_engine_stamp_bytes,
        pair_stop.stop_warm_pair,
    )
    ticks = iter(range(1, 100))
    clock = scc.PhaseClock(now=lambda: float(next(ticks)))

    with clock.wrapped():
        with breadcrumb.hold_publish_marker(tmp_path, holder="t"):
            client.spawn_detached("r", "s")
            client.spawn_detached("r", "s")
            assert breadcrumb.wait_for_successor_answer(tmp_path) == {"pid": 1}
            skew.replace_engine_stamp_bytes(tmp_path, b"old")
            skew.replace_engine_stamp_bytes(tmp_path, b"new")
            assert pair_stop.stop_warm_pair(tmp_path) == "stopped"

    stamps = [clock.epochs[name] for name in scc.PHASES]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert clock.epochs["successor_spawn"] < clock.epochs["old_pair_stop"], "first spawn stamp is kept"
    assert writes == [b"old", b"new"], "the wrapper calls the original"
    assert clock.epochs["token_flip"] > clock.epochs["successor_answer"]
    assert clock.epochs["old_pair_stop_done"] > clock.epochs["old_pair_stop"]
    assert (
        breadcrumb.hold_publish_marker,
        client.spawn_detached,
        breadcrumb.wait_for_successor_answer,
        skew.replace_engine_stamp_bytes,
        pair_stop.stop_warm_pair,
    ) == originals


def test_a_successor_that_never_answers_leaves_no_answer_stamp(monkeypatch) -> None:
    monkeypatch.setattr(breadcrumb, "wait_for_successor_answer", lambda *a, **k: None)
    clock = scc.PhaseClock()
    with clock.wrapped():
        assert breadcrumb.wait_for_successor_answer() is None
    assert "successor_answer" not in clock.epochs


def test_client_pointed_at_resolves_token_and_endpoint_from_the_live_stamp(tmp_path: Path) -> None:
    root = _engine_root(tmp_path)
    stamp = root / "coordinator_core" / skew.ENGINE_STAMP_FILENAME
    before = (client.engine_token, client._endpoint_name, client._engine_clone_root)

    with scc.client_pointed_at(root):
        old_token = client.engine_token()
        assert old_token == skew.compute_client_token(root)
        assert client._endpoint_name(old_token) == lw.endpoint_for(root, old_token)
        assert client._engine_clone_root() == root
        assert client._configured_root_is_foreign() is False
        stamp.write_text("sha:new\n", encoding="utf-8")
        assert client.engine_token() != old_token, "the flip is seen by the next request"
        stamp.unlink()
        assert client.engine_token() == "unversioned"

    assert (client.engine_token, client._endpoint_name, client._engine_clone_root) == before


def test_client_pointed_at_never_spawns(tmp_path: Path) -> None:
    root = _engine_root(tmp_path)
    with scc.client_pointed_at(root):
        assert client._spawn_once("tok", hook_path=True) is None


def test_hook_dispatch_sends_a_hook_path_guard_frame_and_reports_the_request_token(
    monkeypatch, tmp_path: Path
) -> None:
    root = _engine_root(tmp_path)
    sent: list = []

    def fake(msg, **kwargs):
        sent.append((msg, kwargs))
        client.engine_token()
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(client, "try_warm_dispatch", fake)
    dispatch = scc.hook_dispatch(root)
    with scc.client_pointed_at(root, dispatch):
        response, token = dispatch()

    assert response["result"] == {}
    assert token == skew.compute_client_token(root)
    msg, kwargs = sent[0]
    assert kwargs == {"hook_path": True}
    assert msg["method"] == scc.HOOK_METHOD
    assert msg["params"]["payload"]["hook_event_name"] == "PreToolUse"


def test_observe_counts_unanswered_and_error_envelopes_as_no_response() -> None:
    answers = iter([({"result": 1}, "a"), (None, "a"), ({"error": {"code": -1}}, "a")])

    def dispatch():
        return next(answers, ({"result": 1}, "b"))

    report = scc.observe(dispatch, lambda: True, clock=scc.PhaseClock(), pre_roll_secs=0.05, tail_secs=0.02)
    assert report.successor_answered_before_release is True
    assert report.no_response == 2
    assert len(report.requests) > 3
    assert all(r.answer_epoch is not None for r in report.requests if r.token == "b")


def test_observe_runs_the_caller_across_the_whole_swap_and_honours_the_duration_floor() -> None:
    swapping = threading.Event()
    seen_during: list = []

    def dispatch():
        if swapping.is_set():
            seen_during.append(1)
        time.sleep(0.002)
        return {"result": 1}, "t"

    def swap():
        swapping.set()
        time.sleep(0.05)
        swapping.clear()
        return True

    began = time.monotonic()
    report = scc.observe(
        dispatch, swap, clock=scc.PhaseClock(), duration_s=0.4, pre_roll_secs=0.02, tail_secs=0.01
    )
    assert time.monotonic() - began >= 0.4
    assert len(seen_during) >= 3, "requests are issued back to back while the swap runs"
    assert report.no_response == 0


def test_run_wires_caller_phase_clock_and_token_flip_through_the_swap(monkeypatch, tmp_path: Path) -> None:
    root = _engine_root(tmp_path)
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    monkeypatch.setattr(scc, "PRE_ROLL_SECS", 0.05)
    monkeypatch.setattr(scc, "TAIL_AFTER_RELEASE_SECS", 0.05)

    @contextlib.contextmanager
    def fixture(_base, _source):
        yield root

    def handover(engine_root, *, copy_secs):
        with breadcrumb.hold_publish_marker(engine_root, holder="test"):
            time.sleep(0.05)
            for _ in range(200):  # Windows refuses a replace while the caller has the stamp open
                try:
                    skew.replace_engine_stamp_bytes(engine_root, b"sha:new\n")
                    break
                except PermissionError:
                    time.sleep(0.005)
            time.sleep(0.05)
        return True

    def fake_dispatch(msg, **kwargs):
        client.engine_token()
        time.sleep(0.002)
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(scc, "fixture_pair", fixture)
    monkeypatch.setattr(lw, "production_handover", handover)
    monkeypatch.setattr(client, "try_warm_dispatch", fake_dispatch)

    report = scc.run(copy_secs=0.0, work_dir=tmp_path, caller_in_process=True)

    assert report.successor_answered_before_release is True
    assert report.no_response == 0
    versioned = {r.token for r in report.requests} - {"unversioned"}  # a read inside the replace window
    assert len(versioned) == 2, "tokens before and after the flip"
    first_send = min(r.send_epoch for r in report.requests)
    assert first_send < report.phases["marker_take"] < report.phases["token_flip"] < report.phases["marker_release"]
    assert max(r.send_epoch for r in report.requests) > report.phases["marker_release"]


def test_main_prints_the_falsifier_line_and_one_line_per_phase(monkeypatch, capsys) -> None:
    phases = {name: 10.0 + i for i, name in enumerate(scc.PHASES)}
    report = scc.SwapReport(requests=(_req(10.0, 0.01),), phases=phases, successor_answered_before_release=True)
    monkeypatch.setattr(scc, "run", lambda **kw: report)

    assert scc.main([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("swap_continuous_caller: max_gap_ms=")
    assert len(out) == 1 + len(scc.PHASES)


def test_main_assert_budget_exits_one_on_a_no_response_and_zero_when_clean(monkeypatch) -> None:
    dirty = scc.SwapReport(requests=(_req(1.0, 0.01), _req(1.1, None)))
    clean = scc.SwapReport(requests=(_req(1.0, 0.01), _req(1.1, 0.01)))
    monkeypatch.setattr(scc, "run", lambda **kw: dirty)
    assert scc.main(["--assert-budget"]) == 1
    assert scc.main([]) == 0
    monkeypatch.setattr(scc, "run", lambda **kw: clean)
    assert scc.main(["--assert-budget"]) == 0


def test_process_caller_runs_its_loop_in_another_process_and_returns_the_records(tmp_path: Path) -> None:
    """The child aims `client` at a root with no server: every request is a miss, counted as
    `no_response`, and the records cross the process boundary with the child's pid-free epochs."""
    root = _engine_root(tmp_path)
    caller = scc.ProcessCaller(root)
    caller.start()
    time.sleep(0.3)
    records = caller.stop()

    assert len(records) >= 1
    assert all(isinstance(r, scc.Request) and r.answer_epoch is None for r in records)
    assert not caller._proc.is_alive()
