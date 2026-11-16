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
    assert len(out) == 1 + len(scc.PHASES) + 2  # falsifier line, one line per phase, locus, flip window
    assert out[-2].startswith("  longest gap:")
    assert out[-1].startswith("  max gap across token_flip..old_pair_stop:")


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


class _SkewThenAnswer:
    """`_open_pipe` stand-in: the first pipe answers ENGINE_SKEW (the retired generation), the next
    answers a result. Records each endpoint dialed."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.dialed: list = []

    def __call__(self, pipe: str):
        import json

        self.dialed.append(pipe)
        line = json.dumps(self.answers.pop(0)).encode("utf-8") + b"\n"

        class _Pipe:
            def write(self, data: bytes) -> None:
                pass

            def flush(self) -> None:
                pass

            def readline(self) -> bytes:
                return line

            def close(self) -> None:
                pass

        return _Pipe()


_SKEW = {"jsonrpc": "2.0", "id": 1, "error": {"code": client.ENGINE_SKEW, "message": "skew"}}
_OK = {"jsonrpc": "2.0", "id": 1, "result": {}}


def _aim_client(monkeypatch, tokens: tuple, opener) -> None:
    """The token reads `tokens[0]` until the first pipe is dialed, `tokens[-1]` after."""
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: tokens[-1] if opener.dialed else tokens[0])
    monkeypatch.setattr(client, "_endpoint_name", lambda tok: f"pipe-{tok}")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "_configured_root_is_foreign", lambda: False)
    monkeypatch.setattr(client, "_open_pipe", opener)


def test_a_skew_refusal_after_the_flip_is_re_sent_to_the_successor_not_taken_cold(monkeypatch) -> None:
    """A request in flight at the token flip is refused by the retiring generation; the token has
    moved, so it is re-dialed on the successor's endpoint and answered, not sent cold."""
    opener = _SkewThenAnswer([_SKEW, _OK])
    _aim_client(monkeypatch, ("old", "new"), opener)

    response = client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

    assert response == _OK
    assert opener.dialed == ["pipe-old", "pipe-new"]


def test_a_skew_refusal_with_an_unmoved_token_still_goes_cold(monkeypatch) -> None:
    opener = _SkewThenAnswer([_SKEW])
    _aim_client(monkeypatch, ("old",), opener)

    response = client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})

    assert response is None
    assert opener.dialed == ["pipe-old"]


_ALLOW = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}\n'
_FAIL_OPEN = (
    '{"systemMessage":"coordinator: engine failed (no response within 8000ms of delivery); '
    'guard not evaluated, command allowed."}\n'
)


def _door_run(monkeypatch, *, stdout: str, returncode: int = 0) -> scc.Dispatch:
    """`door_dispatch` over a stand-in door process printing `stdout`."""
    import subprocess

    monkeypatch.setattr(
        scc,
        "_run_door",
        lambda argv, **kw: (subprocess.CompletedProcess(argv, returncode, stdout.encode("utf-8"), b""), 0.0),
    )
    monkeypatch.setattr(skew, "compute_client_token", lambda root: "tok")
    return scc.door_dispatch(Path("root"), Path("door.exe"))


def test_door_dispatch_answers_on_a_zero_exit_hook_envelope(monkeypatch) -> None:
    response, token = _door_run(monkeypatch, stdout=_ALLOW)()
    assert "error" not in response and token == "tok"


def test_door_dispatch_counts_the_fail_open_advisory_and_a_nonzero_exit_as_no_response(monkeypatch) -> None:
    advisory, _ = _door_run(monkeypatch, stdout=_FAIL_OPEN)()
    assert "no response within 8000ms of delivery" in advisory["error"]
    crashed, _ = _door_run(monkeypatch, stdout="", returncode=3)()
    assert "error" in crashed


def test_door_dispatch_reports_the_failure_text_the_caller_loop_records() -> None:
    out: list = []
    stop = threading.Event()
    seen = iter([({"error": "connection closed or read failed after delivery"}, "t")])

    def dispatch():
        try:
            return next(seen)
        finally:
            stop.set()

    scc._caller(dispatch, stop, out)
    report = scc.SwapReport(requests=tuple(out))
    assert report.no_response == 1
    assert report.failure_lines() == ["  unanswered x1: connection closed or read failed after delivery"]


def test_door_binary_installs_the_door_under_its_entrypoint_name_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scc.sys, "platform", "win32")
    root = tmp_path / "engine"
    root.mkdir()
    installed = scc.door_binary(root)
    if installed is None:
        pytest.skip("no door image in this checkout")
    assert installed.name == "coordinator-invoke.exe" and installed.is_file()


def test_door_binary_is_none_off_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(scc.sys, "platform", "linux")
    assert scc.door_binary(tmp_path) is None


def test_gap_locus_names_the_phases_inside_the_longest_gap() -> None:
    phases = {"marker_take": 100.0, "successor_spawn": 100.5, "successor_answer": 101.2}
    report = scc.SwapReport(
        requests=(_req(100.1, 0.0), _req(100.2, 0.0), _req(101.5, 0.0)), phases=phases
    )
    locus = report.gap_locus()
    assert "+200.0ms to +1500.0ms" in locus and "successor_spawn, successor_answer" in locus


def test_flip_window_gaps_split_the_handover_gap_from_the_gap_the_box_has_anyway() -> None:
    phases = {"marker_take": 100.0, "token_flip": 101.0, "old_pair_stop_done": 101.4, "marker_release": 101.4}
    report = scc.SwapReport(
        requests=(_req(100.1, 0.0), _req(100.5, 0.0), _req(100.95, 0.0), _req(101.2, 0.0), _req(101.5, 0.0), _req(101.55, 0.0)),
        phases=phases,
    )
    inside, outside = report.flip_window_gaps()
    assert (inside, outside) == (300.0, 450.0)
    assert "300ms" in report.flip_window_line() and "450ms" in report.flip_window_line()


def test_flip_window_gaps_are_all_outside_when_the_flip_was_never_reached() -> None:
    report = scc.SwapReport(requests=(_req(100.0, 0.0), _req(100.4, 0.0)), phases={"marker_take": 100.0})
    assert report.flip_window_gaps() == (0.0, 400.0)


def test_a_pin_back_to_the_old_stamp_is_no_token_flip(monkeypatch, tmp_path: Path) -> None:
    root = _engine_root(tmp_path)
    skew.replace_engine_stamp_bytes(root, b"sha:old\n")
    ticks = iter(range(1, 100))
    clock = scc.PhaseClock(now=lambda: float(next(ticks)))
    with clock.wrapped(root):
        skew.replace_engine_stamp_bytes(root, b"sha:old\n")
        assert "token_flip" not in clock.epochs, "the pin restores the token clients already hold"
        skew.replace_engine_stamp_bytes(root, b"sha:new\n")
    assert "token_flip" in clock.epochs


class _HangingPipe:
    """A warm endpoint whose reply never comes until `release`."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def write(self, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        self._release.wait(10.0)
        return b'{"jsonrpc": "2.0", "id": "successor-probe", "result": {}}\n'

    def close(self) -> None:
        pass


def test_the_successor_gate_keeps_one_probe_in_flight(monkeypatch, tmp_path: Path) -> None:
    opened: list = []
    release = threading.Event()

    def open_pipe(endpoint):
        opened.append(endpoint)
        return _HangingPipe(release)

    monkeypatch.setattr(client, "_open_pipe", open_pipe)
    monkeypatch.setattr(
        breadcrumb, "read_breadcrumb", lambda root=None: {"pid": 20, "pipe": "new", "stable_pid_start_epoch": 2}
    )
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda pid, stored_start_epoch="": True)
    polls = iter(range(6))

    def sleep(_secs: float) -> None:
        if next(polls) == 4:
            release.set()
            time.sleep(0.2)

    answer = breadcrumb.wait_for_successor_answer(
        tmp_path, token="tok", superseded_pid=10, max_secs=60.0, sleep=sleep
    )
    assert len(opened) == 2, "polls while a probe was unanswered sent nothing; the next poll after it replied sent one"
    assert answer is not None


def test_a_probe_in_flight_belongs_to_one_gate_call(monkeypatch) -> None:
    opened: list = []
    release = threading.Event()
    monkeypatch.setattr(client, "_open_pipe", lambda e: opened.append(e) or _HangingPipe(release))
    try:
        assert breadcrumb.endpoint_answers_request("e", "tok", deadline_secs=0.01) is False
        assert breadcrumb.endpoint_answers_request("e", "tok", deadline_secs=0.01) is False
    finally:
        release.set()
    assert len(opened) == 2, "outside a gate call every probe is sent"
