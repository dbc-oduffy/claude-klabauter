"""Land-swap hook-window falsifier: the longest stretch a hook request goes unanswered while a
publish land swaps the engine pair.

Drives the production hold/stop/release primitives (`breadcrumb.hold_publish_marker`,
`pair_stop.stop_warm_pair`, `breadcrumb.wait_for_publish_release`, `breadcrumb.wait_for_new_pair_ready`)
against a tmp engine root while a 0.1s pinger issues modelled hook requests. The engine pair is a
fake (a timer that starts answering `boot_secs` after a spawn); the hook request is a model of the
hook path, selected by `hook_path`, because the real door is a native binary.

A request counts as answered iff it returned an answer within `DRAIN_WAIT_SECS` (+ scheduler slack).
`max_unanswered_secs` is the longest run of consecutive unanswered requests, first start to last end.
`slow_answers` counts requests that outlasted the budget whatever their outcome.

Usage: python coordinator_core/benchmarks/land_swap_hook_window.py [--respawn pre-release]
[--hook-path bounded] [--assert-budget]. Exit 1 only under --assert-budget when the window or the
slow count exceeds the budget.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.warm import breadcrumb, pair_stop

RESPAWN_POST_RELEASE = "post-release"
RESPAWN_PRE_RELEASE = "pre-release"

HOOK_PATH_BASE = "base"
HOOK_PATH_BOUNDED = "bounded"

PING_INTERVAL_SECS = 0.1
#: Scheduler slack on top of `DRAIN_WAIT_SECS` before a request counts as slow.
BUDGET_SLACK_SECS = 0.05
#: The client's boot-wait deadline as measured on the live box (~2.13s per hook ping).
BOOT_WAIT_SECS = 2.13
DEFAULT_COPY_SECS = 2.0
DEFAULT_BOOT_SECS = 3.0
DEFAULT_PRE_ROLL_SECS = 0.3
DEFAULT_TAIL_SECS = 0.5
READY_MAX_SECS = 30.0

_RUNTIME_BASE_ENV = breadcrumb.RUNTIME_BASE_ENV


@dataclass(frozen=True)
class Sample:
    started: float
    elapsed: float
    answered: bool


@dataclass(frozen=True)
class WindowReport:
    max_unanswered_secs: float
    slow_answers: int
    requests: int
    respawn: str
    hook_path: str

    def within_budget(self) -> bool:
        return self.max_unanswered_secs <= breadcrumb.DRAIN_WAIT_SECS and self.slow_answers == 0


class FakePair:
    """An engine pair that answers from `boot_secs` after a spawn until stopped."""

    def __init__(self, boot_secs: float) -> None:
        self._boot_secs = boot_secs
        self._lock = threading.Lock()
        self._answering = threading.Event()
        self._booting = False
        self._timers: list[threading.Timer] = []

    @property
    def answering(self) -> bool:
        return self._answering.is_set()

    def start_answering(self) -> None:
        self._answering.set()

    def spawn(self) -> bool:
        """Begin a boot unless one is running or the pair already answers."""
        with self._lock:
            if self._booting or self._answering.is_set():
                return False
            self._booting = True
            timer = threading.Timer(self._boot_secs, self._finish_boot)
            timer.daemon = True
            self._timers.append(timer)
            timer.start()
            return True

    def _finish_boot(self) -> None:
        with self._lock:
            self._booting = False
            self._answering.set()

    def stop(self) -> None:
        with self._lock:
            self._answering.clear()
            self._booting = False
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()


def hook_request(
    pair: FakePair,
    engine_root: Path,
    hook_path: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """One modelled hook request; True iff it returned an answer.

    base: pair up -> answered. Marker held and pair down -> drain up to `DRAIN_WAIT_SECS`, then a
    refusal. Marker free and pair down -> spawn (debounced) and wait the client boot deadline.
    bounded: as base, but a held marker ends in a degraded answer within the drain budget.
    """
    if pair.answering:
        return True
    if breadcrumb.publish_marker_held(breadcrumb.svc_dir(engine_root)):
        breadcrumb.wait_for_publish_release(engine_root)
        if pair.answering:
            return True
        return hook_path == HOOK_PATH_BOUNDED
    pair.spawn()
    deadline = clock() + BOOT_WAIT_SECS
    while clock() < deadline:
        if pair.answering:
            return True
        sleep(breadcrumb.DRAIN_POLL_SECS)
    return hook_path == HOOK_PATH_BOUNDED


def summarize(samples: list[Sample], *, respawn: str = "", hook_path: str = "") -> WindowReport:
    """Reduce pinger samples to the window report."""
    budget = breadcrumb.DRAIN_WAIT_SECS + BUDGET_SLACK_SECS
    slow = sum(1 for s in samples if s.elapsed > budget)
    longest = 0.0
    run_start: Optional[float] = None
    run_end = 0.0
    for s in samples:
        if s.answered and s.elapsed <= budget:
            if run_start is not None:
                longest = max(longest, run_end - run_start)
                run_start = None
            continue
        if run_start is None:
            run_start = s.started
        run_end = s.started + s.elapsed
    if run_start is not None:
        longest = max(longest, run_end - run_start)
    return WindowReport(
        max_unanswered_secs=round(longest, 3),
        slow_answers=slow,
        requests=len(samples),
        respawn=respawn,
        hook_path=hook_path,
    )


def _pinger(
    pair: FakePair,
    engine_root: Path,
    hook_path: str,
    interval: float,
    stop: threading.Event,
    out: list[Sample],
) -> None:
    origin = time.monotonic()
    while not stop.is_set():
        began = time.monotonic()
        answered = hook_request(pair, engine_root, hook_path)
        elapsed = time.monotonic() - began
        out.append(Sample(started=began - origin, elapsed=elapsed, answered=answered))
        stop.wait(max(0.0, interval - elapsed))


def run_swap(
    *,
    copy_secs: float = DEFAULT_COPY_SECS,
    boot_secs: float = DEFAULT_BOOT_SECS,
    respawn: str = RESPAWN_POST_RELEASE,
    hook_path: str = HOOK_PATH_BASE,
    ping_interval: float = PING_INTERVAL_SECS,
    pre_roll_secs: float = DEFAULT_PRE_ROLL_SECS,
    tail_secs: float = DEFAULT_TAIL_SECS,
    stop_pair: Optional[Callable[[Path], object]] = None,
    work_dir: Optional[Path] = None,
) -> WindowReport:
    """Run one land swap against a tmp engine root and report the hook window.

    The swap: hold the publish marker, stop the old pair, copy (`copy_secs`), then either release
    and let the first hook caller spawn (`post-release`), or spawn and wait for the new pair to
    answer before releasing (`pre-release`). `stop_pair` defaults to the production
    `pair_stop.stop_warm_pair`.
    """
    stopper = stop_pair if stop_pair is not None else pair_stop.stop_warm_pair
    with tempfile.TemporaryDirectory(prefix="land-swap-hook-window-", dir=work_dir) as tmp:
        root = Path(tmp) / "engine"
        root.mkdir()
        saved = os.environ.get(_RUNTIME_BASE_ENV)
        os.environ[_RUNTIME_BASE_ENV] = str(Path(tmp) / "runtime")
        pair = FakePair(boot_secs)
        pair.start_answering()
        samples: list[Sample] = []
        stop_ping = threading.Event()
        thread = threading.Thread(
            target=_pinger,
            args=(pair, root, hook_path, ping_interval, stop_ping, samples),
            daemon=True,
        )
        try:
            thread.start()
            time.sleep(pre_roll_secs)
            with breadcrumb.hold_publish_marker(root, holder="land_swap_hook_window"):
                stopper(root)
                pair.stop()
                time.sleep(copy_secs)
                if respawn == RESPAWN_PRE_RELEASE:
                    pair.spawn()
                    breadcrumb.wait_for_new_pair_ready(
                        root, READY_MAX_SECS, ping=lambda: pair.answering
                    )
            deadline = time.monotonic() + boot_secs + BOOT_WAIT_SECS + 1.0
            while not pair.answering and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(tail_secs)
        finally:
            stop_ping.set()
            thread.join(timeout=BOOT_WAIT_SECS + 2.0)
            pair.stop()
            if saved is None:
                os.environ.pop(_RUNTIME_BASE_ENV, None)
            else:
                os.environ[_RUNTIME_BASE_ENV] = saved
        return summarize(list(samples), respawn=respawn, hook_path=hook_path)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--copy-secs", type=float, default=DEFAULT_COPY_SECS)
    parser.add_argument("--boot-secs", type=float, default=DEFAULT_BOOT_SECS)
    parser.add_argument(
        "--respawn", choices=(RESPAWN_POST_RELEASE, RESPAWN_PRE_RELEASE), default=RESPAWN_POST_RELEASE
    )
    parser.add_argument("--hook-path", choices=(HOOK_PATH_BASE, HOOK_PATH_BOUNDED), default=HOOK_PATH_BASE)
    parser.add_argument("--assert-budget", action="store_true")
    args = parser.parse_args(argv)
    report = run_swap(
        copy_secs=args.copy_secs,
        boot_secs=args.boot_secs,
        respawn=args.respawn,
        hook_path=args.hook_path,
    )
    print(f"max_unanswered_secs={report.max_unanswered_secs}")
    print(f"slow_answers={report.slow_answers}")
    print(json.dumps(asdict(report), sort_keys=True))
    if args.assert_budget and not report.within_budget():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
