"""Continuous-caller swap harness: a hook-shaped caller issues requests back to back across one
production publish handover and the report timestamps every phase of the swap.

A fixture engine root (a hard-linked copy of this checkout) gets a warm pair in a throwaway
`COORDINATOR_WARM_RUNTIME_BASE`. One caller process (a hook is a process of its own) sends `client.try_warm_dispatch` hook frames
(`hook_path=True`) with no pause from before the marker take to `TAIL_AFTER_RELEASE_SECS` after the
marker release, resolving the token and endpoint from the fixture root's live stamp on every request
as a client does. The swap side is `land_swap_hook_window.production_handover`, which runs
`round.start_successor_pair_in_hold`; phase epochs come from wrapping `breadcrumb.hold_publish_marker`,
`client.spawn_detached`, `breadcrumb.wait_for_successor_answer`, `skew.replace_engine_stamp_bytes`
and `pair_stop.stop_warm_pair` for the length of the swap.

Units. `max_gap_ms` is wall between consecutive answers. `p50_ms` is the median wall round trip of
answered requests. `max_process_ms` is the largest per-request CPU time of the calling thread
(`time.thread_time`): the DR-344 bar is process time, never wall. It is the caller's own cost;
server-side CPU is not in it. `no_response` counts requests that got no evaluated answer.

Run: `python -m coordinator_core.benchmarks.swap_continuous_caller [--duration-s N] [--assert-budget]`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import land_swap_hook_window as lw
from coordinator_core.benchmarks import warm_pair_boot_process_time as boot
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.warm import breadcrumb, client, pair_stop, skew

PHASES = (
    "marker_take",
    "successor_spawn",
    "successor_answer",
    "token_flip",
    "old_pair_stop",
    "marker_release",
)
PRE_ROLL_SECS = 0.3
TAIL_AFTER_RELEASE_SECS = 2.0
#: Pause after a request that got no answer, so a missing server is not a busy loop.
MISS_BACKOFF_SECS = 0.001
GAP_BUDGET_MS = 500.0
PROCESS_BUDGET_MS = 200.0
HOOK_METHOD = "warm_guard.evaluate"

#: One hook round trip: its response (None when nothing answered) and the token it was stamped with.
Dispatch = Callable[[], "tuple[Optional[dict], str]"]


@dataclass(frozen=True)
class Request:
    """One caller request; epochs are wall-clock seconds, `answer_epoch` None when unanswered."""

    send_epoch: float
    answer_epoch: Optional[float]
    process_ms: float
    token: str


@dataclass(frozen=True)
class SwapReport:
    """The caller's records and the swap's phase epochs (`PHASES` plus `old_pair_stop_done`)."""

    requests: tuple
    phases: dict = field(default_factory=dict)
    successor_answered_before_release: Optional[bool] = None

    @property
    def answered(self) -> list:
        return [r for r in self.requests if r.answer_epoch is not None]

    @property
    def no_response(self) -> int:
        return sum(1 for r in self.requests if r.answer_epoch is None)

    @property
    def max_gap_ms(self) -> float:
        epochs = sorted(r.answer_epoch for r in self.answered)
        return round(max((b - a for a, b in zip(epochs, epochs[1:])), default=0.0) * 1000.0, 1)

    @property
    def max_process_ms(self) -> float:
        return round(max((r.process_ms for r in self.requests), default=0.0), 1)

    @property
    def p50_ms(self) -> float:
        rtts = [(r.answer_epoch - r.send_epoch) * 1000.0 for r in self.answered]
        return round(statistics.median(rtts), 1) if rtts else 0.0

    def within_budget(self) -> bool:
        return (
            self.no_response == 0
            and self.max_gap_ms < GAP_BUDGET_MS
            and self.max_process_ms < PROCESS_BUDGET_MS
        )

    def falsifier_line(self) -> str:
        return (
            f"swap_continuous_caller: max_gap_ms={self.max_gap_ms:g} no_response={self.no_response} "
            f"max_process_ms={self.max_process_ms:g} p50_ms={self.p50_ms:g} "
            f"requests={len(self.requests)} "
            f"successor_answered_before_release={self.successor_answered_before_release}"
        )

    def timeline_lines(self) -> list:
        """One line per phase: ms since `marker_take`; `old_pair_stop` also names its end."""
        origin = self.phases.get("marker_take")
        lines = []
        for name in PHASES:
            epoch = self.phases.get(name)
            if epoch is None or origin is None:
                lines.append(f"  phase {name:<17} not reached")
                continue
            line = f"  phase {name:<17} +{(epoch - origin) * 1000.0:9.1f}ms"
            done = self.phases.get(f"{name}_done")
            if done is not None:
                line += f" (done +{(done - origin) * 1000.0:.1f}ms)"
            lines.append(line)
        return lines


class PhaseClock:
    """Stamps `PHASES` epochs and `old_pair_stop_done`; a phase seen twice keeps its first stamp,
    except `token_flip`, which is the last stamp write (the first, when a pin precedes it, restores
    the old stamp for the successor's boot)."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self.epochs: dict = {}
        self._lock = threading.Lock()

    def mark(self, name: str, *, last: bool = False) -> None:
        with self._lock:
            if last or name not in self.epochs:
                self.epochs[name] = self._now()

    @contextlib.contextmanager
    def wrapped(self) -> Iterator[None]:
        """Wrap the swap's primitives for the length of the block. Each wrapper calls the original."""
        orig_marker = breadcrumb.hold_publish_marker
        orig_spawn = client.spawn_detached
        orig_wait = breadcrumb.wait_for_successor_answer
        orig_stamp = skew.replace_engine_stamp_bytes
        orig_stop = pair_stop.stop_warm_pair

        @contextlib.contextmanager
        def marker(engine_root, *, holder):
            with orig_marker(engine_root, holder=holder) as path:
                self.mark("marker_take")
                yield path
            self.mark("marker_release")

        def spawn(*args, **kwargs):
            self.mark("successor_spawn")
            return orig_spawn(*args, **kwargs)

        def wait(*args, **kwargs):
            answer = orig_wait(*args, **kwargs)
            if answer is not None:
                self.mark("successor_answer")
            return answer

        def stamp(*args, **kwargs):
            result = orig_stamp(*args, **kwargs)
            self.mark("token_flip", last=True)
            return result

        def stop(*args, **kwargs):
            self.mark("old_pair_stop")
            try:
                return orig_stop(*args, **kwargs)
            finally:
                self.mark("old_pair_stop_done")

        with contextlib.ExitStack() as stack:
            for module, attr, fn in (
                (breadcrumb, "hold_publish_marker", marker),
                (client, "spawn_detached", spawn),
                (breadcrumb, "wait_for_successor_answer", wait),
                (skew, "replace_engine_stamp_bytes", stamp),
                (pair_stop, "stop_warm_pair", stop),
            ):
                stack.enter_context(mock.patch.object(module, attr, fn))
            yield


def _caller(dispatch: Dispatch, stop: threading.Event, out: list) -> None:
    while not stop.is_set():
        send = time.time()
        cpu0 = time.thread_time()
        response, token = dispatch()
        process_ms = (time.thread_time() - cpu0) * 1000.0
        answered = isinstance(response, dict) and "error" not in response
        out.append(Request(send, time.time() if answered else None, process_ms, token))
        if not answered:
            stop.wait(MISS_BACKOFF_SECS)


class ThreadCaller:
    """The caller as a thread of this process: for a stand-in `dispatch` in a unit test."""

    def __init__(self, dispatch: Dispatch) -> None:
        self._dispatch = dispatch
        self._stop = threading.Event()
        self._records: list = []
        self._thread = threading.Thread(
            target=_caller, args=(dispatch, self._stop, self._records), daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> list:
        self._stop.set()
        self._thread.join(timeout=lw.BOOT_WAIT_SECS + 2.0)
        return self._records


def _caller_process_main(root: str, ready, stop, out) -> None:
    """Body of the `ProcessCaller` child: aim `client` at `root`, signal ready, loop until `stop`."""
    path = Path(root)
    dispatch = hook_dispatch(path)
    records: list = []
    with client_pointed_at(path, dispatch):
        ready.set()
        _caller(dispatch, stop, records)
    out.put(records)


class ProcessCaller:
    """The caller as its own process, as a hook is in production: the swap's CPU (process
    enumeration under the publisher's GIL) never shares an interpreter with the requests it is
    measured against, so a gap is the swap's effect on a client and not a GIL stall of this harness."""

    def __init__(self, root: Path) -> None:
        import multiprocessing

        ctx = multiprocessing.get_context("spawn")
        self._ready = ctx.Event()
        self._stop = ctx.Event()
        self._out = ctx.Queue()
        self._proc = ctx.Process(
            target=_caller_process_main,
            args=(str(root), self._ready, self._stop, self._out),
            daemon=True,
        )

    def start(self) -> None:
        self._proc.start()
        if not self._ready.wait(lw.BOOT_WAIT_SECS):
            self._proc.terminate()
            raise RuntimeError("caller process never became ready")

    def stop(self) -> list:
        import queue

        self._stop.set()
        try:
            records = self._out.get(timeout=lw.BOOT_WAIT_SECS + 2.0)
        except queue.Empty:
            records = []
        self._proc.join(timeout=5.0)
        if self._proc.is_alive():
            self._proc.terminate()
        return records


def observe(
    dispatch: Optional[Dispatch],
    swap: Callable[[], Optional[bool]],
    *,
    clock: PhaseClock,
    duration_s: Optional[float] = None,
    pre_roll_secs: Optional[float] = None,
    tail_secs: Optional[float] = None,
    caller: Optional[object] = None,
) -> SwapReport:
    """Run `swap` with a caller hitting its target back to back around it, and report.

    `caller` is a started-and-stopped caller (`ThreadCaller(dispatch)` when absent, or a
    `ProcessCaller`). It starts `pre_roll_secs` before `swap` and runs until `tail_secs` after it
    returns, or until `duration_s` from its start when that is later."""
    pre_roll_secs = PRE_ROLL_SECS if pre_roll_secs is None else pre_roll_secs
    tail_secs = TAIL_AFTER_RELEASE_SECS if tail_secs is None else tail_secs
    caller = caller if caller is not None else ThreadCaller(dispatch)  # type: ignore[arg-type]
    records: list = []
    answered_before_release: Optional[bool] = None
    caller.start()
    began = time.monotonic()
    try:
        time.sleep(pre_roll_secs)
        answered_before_release = swap()
        time.sleep(tail_secs)
        if duration_s is not None:
            time.sleep(max(0.0, duration_s - (time.monotonic() - began)))
    finally:
        records = caller.stop()
    return SwapReport(
        requests=tuple(records),
        phases=dict(clock.epochs),
        successor_answered_before_release=answered_before_release,
    )


def hook_dispatch(root: Path) -> Dispatch:
    """The dispatch for `root`: a `PreToolUse` guard frame through `client.try_warm_dispatch` with
    `hook_path=True`. Requires `client_pointed_at(root)` to be active."""
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": HOOK_METHOD,
        "params": {
            "payload": {
                "hook_event_name": "PreToolUse",
                "session_id": "swap-continuous-caller",
                "tool_name": "Bash",
                "tool_input": {"command": "echo ok"},
                "cwd": str(root),
            }
        },
    }
    seen = threading.local()

    def dispatch() -> tuple:
        response = client.try_warm_dispatch(dict(frame), hook_path=True)
        return response, getattr(seen, "token", "")

    dispatch.seen = seen  # type: ignore[attr-defined]
    return dispatch


@contextlib.contextmanager
def client_pointed_at(root: Path, dispatch: Optional[Dispatch] = None) -> Iterator[None]:
    """Aim `client` at `root`: token from its live stamp each request, its endpoint, no spawn, no
    foreign-root refusal. A request that cannot read the stamp goes out as `unversioned`, as the
    client's own fallback does."""
    sid = None
    if sys.platform == "win32":
        from coordinator_core.warm import election

        sid = election.current_user_sid()
    seen = getattr(dispatch, "seen", threading.local())

    def token() -> str:
        try:
            value = skew.compute_client_token(root)
        except Exception:  # noqa: BLE001 -- the client's own fallback for an unreadable stamp
            value = "unversioned"
        seen.token = value
        return value

    with contextlib.ExitStack() as stack:
        for attr, value in (
            ("_live_tree_cold", False),
            ("engine_token", token),
            ("_configured_root_is_foreign", lambda: False),
            ("_engine_clone_root", lambda: root),
            ("_endpoint_name", lambda tok: lw.endpoint_for(root, tok, sid)),
            ("_spawn_once", lambda *a, **k: None),
        ):
            stack.enter_context(mock.patch.object(client, attr, value))
        yield


@contextlib.contextmanager
def fixture_pair(work_dir: Path, source_root: Path) -> Iterator[Path]:
    """A fixture engine root with an answering warm pair, in a throwaway runtime base and settings
    home under `work_dir`. The pair is stopped and the tree removed on exit."""
    from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
    from coordinator_core.temp_layout import mkdtemp_in

    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = mkdtemp_in("swap-continuous-caller-", work_dir)
    pair: Optional[lw.RealPair] = None
    try:
        declare_benchmark_origin()
        root = boot.build_engine_root(tmp / "engine", source_root)
        with contextlib.ExitStack() as env:
            env.enter_context(boot.isolated_runtime(tmp / "runtime"))
            env.enter_context(fixture_settings_home(tmp))
            env.enter_context(mock.patch.dict(os.environ, {"COORDINATOR_WARM": "1"}))
            pair = lw.RealPair(root)
            pair.boot()
            if not lw.wait_until_answering(pair):
                raise RuntimeError("old pair never answered a ping")
            yield root
    finally:
        if pair is not None:
            pair.stop()
        rmtree_or_raise(tmp, label="swap-continuous-caller")


def run(
    *,
    duration_s: Optional[float] = None,
    copy_secs: float = lw.DEFAULT_COPY_SECS,
    work_dir: Optional[Path] = None,
    source_root: Optional[Path] = None,
    caller_in_process: bool = False,
) -> SwapReport:
    """One production handover against a real pair in a throwaway engine root, with a continuous
    hook-shaped caller across it. `duration_s` is a floor on the caller's total run time. The
    caller is its own process (`ProcessCaller`) unless `caller_in_process`, which a unit test uses
    to stand in for the client."""
    source = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    base = work_dir if work_dir is not None else source / "scratch"
    with fixture_pair(base, source) as root:
        dispatch = hook_dispatch(root)
        clock = PhaseClock()
        caller = None if caller_in_process else ProcessCaller(root)
        aim = client_pointed_at(root, dispatch) if caller_in_process else contextlib.nullcontext()
        with aim, clock.wrapped():
            report = observe(
                dispatch,
                lambda: lw.production_handover(root, copy_secs=copy_secs),
                clock=clock,
                duration_s=duration_s,
                caller=caller,
            )
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration-s", type=float, default=None)
    parser.add_argument("--copy-secs", type=float, default=lw.DEFAULT_COPY_SECS)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--assert-budget", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    report = run(duration_s=args.duration_s, copy_secs=args.copy_secs, work_dir=args.work_dir)
    print(report.falsifier_line())
    for line in report.timeline_lines():
        print(line)
    return 1 if args.assert_budget and not report.within_budget() else 0


if __name__ == "__main__":
    sys.exit(main())
