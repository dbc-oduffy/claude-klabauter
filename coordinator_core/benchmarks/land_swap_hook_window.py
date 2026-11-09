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
`unevaluated_guard_count` counts requests that got no evaluated answer in budget: each one is a
guard that failed open or was blocked on the real box.

Two arms. `production` (default) boots a real warm server from a tmp engine root and drives the
production handover order (pre-stop, `hold_publish_marker`, marked stop, tree rewrite, then
`percolate.round.start_successor_pair_in_hold`) while the pinger sends real `ping` ops over the
real endpoint, resolving the token from the stamp each request as the client does. `model` is the
fake-pair arm above. `--spike` answers one question: does the OLD pair keep answering guards while
its tree is replaced, and can a client still reach it once the stamp flips?

Usage: python coordinator_core/benchmarks/land_swap_hook_window.py [--arm model] [--respawn
pre-release] [--hook-path bounded] [--spike] [--assert-budget]. Exit 1 only under --assert-budget
when the window, the slow count or the unevaluated count exceeds the budget.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import warm_pair_boot_process_time as boot
from coordinator_core.warm import breadcrumb, client, pair_stop, skew
from coordinator_core.warm.telemetry import SPAWN_EPOCH_ENV

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
#: The brightline: no stretch longer than this may pass with no pair answering a guard.
UNANSWERED_BUDGET_SECS = 0.5
#: A real ping that has not returned by now reads as unanswered, so a booting server costs a
#: pinger tick this much and not a client's whole read deadline.
PING_TIMEOUT_SECS = 0.25
PAIR_BOOT_MAX_SECS = 120.0

_RUNTIME_BASE_ENV = breadcrumb.RUNTIME_BASE_ENV
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_HOOK_SPAWN_CODE = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from coordinator_core.warm import breadcrumb, client\n"
    "from coordinator_core.warm.telemetry import SPAWN_EPOCH_ENV\n"
    "root = Path(sys.argv[1])\n"
    "if breadcrumb.should_spawn(root) and client.spawn_detached(\n"
    "        str(root), client.SERVER_ENTRY_SCRIPT, env_extra={SPAWN_EPOCH_ENV: repr(time.time())}):\n"
    "    print('spawned')\n"
)


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
    unevaluated_guard_count: int = 0
    successor_answered_before_release: Optional[bool] = None
    phase_secs: dict = field(default_factory=dict)

    def within_budget(self) -> bool:
        return (
            self.max_unanswered_secs <= UNANSWERED_BUDGET_SECS
            and self.slow_answers == 0
            and self.unevaluated_guard_count == 0
        )


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
        unevaluated_guard_count=sum(1 for s in samples if not (s.answered and s.elapsed <= budget)),
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


def endpoint_for(engine_root: Path, token: str, sid: Optional[str] = None) -> str:
    """The endpoint a client holding `token` opens for `engine_root`."""
    from coordinator_core.warm import election

    if sys.platform == "win32":
        return election.pipe_name(token, engine_clone=engine_root, user_sid=sid or election.current_user_sid())
    return str(election.socket_path(token, engine_clone=engine_root))


class RealPair:
    """The real warm server of a tmp engine root, probed the way a guard reaches it.

    `answering` is one `ping` op over the endpoint the CURRENT stamp resolves to, so a pair on a
    superseded token is unreachable here exactly as it is for a client. `spawn` is the client's
    debounced spawn.
    """

    def __init__(self, engine_root: Path) -> None:
        self._root = engine_root
        self._sid: Optional[str] = None
        if sys.platform == "win32":
            from coordinator_core.warm import election

            self._sid = election.current_user_sid()

    def ping(self) -> Optional[dict]:
        """The server's reply to one ping, or None when nothing answered within the timeout."""
        try:
            token = skew.compute_client_token(self._root)
            probe = boot.Probe(endpoint_for(self._root, token, self._sid), boot.PING_METHOD, token)
        except Exception:  # noqa: BLE001 -- an unreadable stamp mid-rewrite is an unanswered ping
            return None
        probe.start()
        probe.done.wait(PING_TIMEOUT_SECS)
        return probe.response if probe.done.is_set() else None

    @property
    def answering(self) -> bool:
        reply = self.ping()
        return reply is not None and "error" not in reply

    def spawn(self) -> bool:
        """A hook caller's spawn, from a short-lived process as in production: `should_spawn`'s boot
        claim is held until its taker exits, so taking it here would debounce the swap's own
        successor spawn."""
        proc = subprocess.run(
            [sys.executable, "-c", _HOOK_SPAWN_CODE, str(self._root)],
            env={**os.environ, "PYTHONPATH": str(_SOURCE_ROOT)},
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        return proc.stdout.strip() == "spawned"

    def boot(self) -> bool:
        """Start the server without taking a boot claim, as a process that came and went before
        the swap did."""
        return client.spawn_detached(
            str(self._root), client.SERVER_ENTRY_SCRIPT, env_extra={SPAWN_EPOCH_ENV: repr(time.time())}
        )

    def stop(self) -> None:
        """Stop the pair by the production stop, then kill whatever the stop left."""
        boot.stop_engine_root(self._root)


def wait_until_answering(pair: RealPair, max_secs: float = PAIR_BOOT_MAX_SECS) -> bool:
    deadline = time.monotonic() + max_secs
    while time.monotonic() < deadline:
        if pair.answering:
            return True
        time.sleep(0.05)
    return False


def rewrite_tree(engine_root: Path, copy_secs: float, *, new_stamp: str = "sha:swapped\n") -> int:
    """Replace every source file under `engine_root` (new inode, new mtime: the shape a publish
    copy leaves, bytecode invalid) and then the stamp, taking at least `copy_secs`. Returns files
    replaced. Files are replaced, never written through: they may be hard links to the checkout."""
    began = time.monotonic()
    count = 0
    package = engine_root / "coordinator_core"
    for path in list(package.rglob("*.py")):
        data = path.read_bytes()
        staged = path.with_name(path.name + ".swap")
        staged.write_bytes(data)
        os.replace(staged, path)
        count += 1
    remaining = copy_secs - (time.monotonic() - began)
    if remaining > 0:
        time.sleep(remaining)
    stamp = package / skew.ENGINE_STAMP_FILENAME
    staged = stamp.with_name(stamp.name + ".swap")
    staged.write_text(new_stamp, encoding="utf-8")
    os.replace(staged, stamp)
    return count


def production_handover(
    engine_root: Path,
    *,
    copy_secs: float,
    ready_budget_secs: Optional[float] = None,
    marks: Optional[dict] = None,
) -> bool:
    """The publish swap order against a real pair: hold the marker with the old pair left
    answering, rewrite the tree, start the successor under the hold, which stops the old pair only
    after the successor answers (`round.start_successor_pair_in_hold`), release.
    True iff the successor answered before the release. `marks`, when given, receives the
    `time.monotonic()` instant of each step."""
    from coordinator_core.percolate import round as round_mod

    marks = marks if marks is not None else {}
    budget = ready_budget_secs if ready_budget_secs is not None else round_mod.NEW_PAIR_READY_BUDGET_SECS
    with breadcrumb.hold_publish_marker(engine_root, holder="land_swap_hook_window"):
        old_stamp = skew.read_engine_stamp_bytes(engine_root)
        rewrite_tree(engine_root, copy_secs)
        marks["copy_done"] = time.monotonic()
        answered = round_mod.start_successor_pair_in_hold(
            engine_root, err=io.StringIO(), ready_budget_secs=budget, old_stamp=old_stamp
        )
        marks["successor_returned"] = time.monotonic()
    marks["released"] = time.monotonic()
    return answered


def run_production_swap(
    *,
    copy_secs: float = DEFAULT_COPY_SECS,
    ping_interval: float = PING_INTERVAL_SECS,
    pre_roll_secs: float = DEFAULT_PRE_ROLL_SECS,
    tail_secs: float = DEFAULT_TAIL_SECS,
    hook_path: str = HOOK_PATH_BASE,
    work_dir: Optional[Path] = None,
    source_root: Optional[Path] = None,
) -> WindowReport:
    """One production handover against a real pair in a tmp engine root, with a hook pinger."""
    source = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    base = work_dir if work_dir is not None else source / "scratch"
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="land-swap-production-", dir=base))
    pair: Optional[RealPair] = None
    try:
        root = boot.build_engine_root(tmp / "engine", source)
        with boot.isolated_runtime(tmp / "runtime"):
            pair = RealPair(root)
            pair.boot()
            if not wait_until_answering(pair):
                raise RuntimeError("old pair never answered a ping")
            samples: list[Sample] = []
            stop_ping = threading.Event()
            thread = threading.Thread(
                target=_pinger, args=(pair, root, hook_path, ping_interval, stop_ping, samples), daemon=True
            )
            released_answering: Optional[bool] = None
            marks: dict = {}
            origin = time.monotonic()
            try:
                thread.start()
                time.sleep(pre_roll_secs)
                released_answering = production_handover(root, copy_secs=copy_secs, marks=marks)
                wait_until_answering(pair, READY_MAX_SECS)
                marks["first_guard_answer"] = time.monotonic()
                time.sleep(tail_secs)
            finally:
                stop_ping.set()
                thread.join(timeout=BOOT_WAIT_SECS + 2.0)
            report = summarize(list(samples), respawn="production", hook_path=hook_path)
            return replace(
                report,
                successor_answered_before_release=released_answering,
                phase_secs={k: round(v - origin, 2) for k, v in marks.items()},
            )
    finally:
        if pair is not None:
            pair.stop()
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass(frozen=True)
class SpikeReport:
    baseline_ok: int
    replaced_files: int
    sequential_ok: int
    sequential_total: int
    skew_refusals: int
    first_skew_after_secs: Optional[float]
    concurrent_ok: int
    concurrent_total: int
    server_alive_after_probes: bool
    failure_samples: tuple
    reachable_after_stamp_flip: bool

    @property
    def verdict(self) -> str:
        """`allowed` only when every guard ping is answered through the replaced tree (no
        ENGINE_SKEW refusal, the server still alive) and the old pair is still reachable after the
        stamp flips."""
        ok = (
            self.baseline_ok > 0
            and self.sequential_ok == self.sequential_total
            and self.concurrent_ok == self.concurrent_total
            and self.skew_refusals == 0
            and self.server_alive_after_probes
            and self.reachable_after_stamp_flip
        )
        return "allowed" if ok else "not-allowed"


def run_old_pair_spike(
    *,
    sequential: int = 30,
    concurrent: int = 12,
    probe_gap_secs: float = 0.2,
    work_dir: Optional[Path] = None,
    source_root: Optional[Path] = None,
) -> SpikeReport:
    """Boot a pair, replace its tree with modules that raise on import, and probe it.

    Sequential pings (`probe_gap_secs` apart, spanning the server's source-staleness refresh
    interval) reach workers that already hold their modules; the server's own skew check reads
    the replaced tree. A concurrent burst follows. The stamp is flipped last to check whether a
    client resolving the new token can still reach the old pair.
    """
    source = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    base = work_dir if work_dir is not None else source / "scratch"
    base.mkdir(parents=True, exist_ok=True)
    import psutil

    tmp = Path(tempfile.mkdtemp(prefix="land-swap-spike-", dir=base))
    pair: Optional[RealPair] = None
    try:
        root = boot.build_engine_root(tmp / "engine", source)
        with boot.isolated_runtime(tmp / "runtime"):
            pair = RealPair(root)
            pair.boot()
            if not wait_until_answering(pair):
                raise RuntimeError("old pair never answered a ping")
            baseline_ok = sum(1 for _ in range(3) if pair.answering)
            replaced = _poison_tree(root)
            failures: list[str] = []
            replies: list[Optional[dict]] = []
            first_skew: Optional[float] = None
            began = time.monotonic()
            for _ in range(sequential):
                reply = pair.ping()
                replies.append(reply)
                if _is_skew(reply) and first_skew is None:
                    first_skew = round(time.monotonic() - began, 2)
                time.sleep(probe_gap_secs)
            seq_ok = sum(1 for r in replies if _is_answer(r))
            failures.extend(repr(r)[:200] for r in replies if not _is_answer(r))
            with ThreadPoolExecutor(max_workers=concurrent) as ex:
                burst = list(ex.map(lambda _i: pair.ping(), range(concurrent)))
            failures.extend(repr(r)[:200] for r in burst if not _is_answer(r))
            crumb = breadcrumb.read_breadcrumb(root)
            alive = crumb is not None and psutil.pid_exists(crumb["pid"])
            (root / "coordinator_core" / skew.ENGINE_STAMP_FILENAME).write_text(
                "sha:spike-new\n", encoding="utf-8"
            )
            reachable = pair.answering
            return SpikeReport(
                baseline_ok=baseline_ok,
                replaced_files=replaced,
                sequential_ok=seq_ok,
                sequential_total=sequential,
                skew_refusals=sum(1 for r in (*replies, *burst) if _is_skew(r)),
                first_skew_after_secs=first_skew,
                concurrent_ok=sum(1 for r in burst if _is_answer(r)),
                concurrent_total=concurrent,
                server_alive_after_probes=alive,
                failure_samples=tuple(failures[:4]),
                reachable_after_stamp_flip=reachable,
            )
    finally:
        if pair is not None:
            pair.stop()
        shutil.rmtree(tmp, ignore_errors=True)


ENGINE_SKEW_CODE = -32002


def _is_answer(reply: Optional[dict]) -> bool:
    return reply is not None and "error" not in reply


def _is_skew(reply: Optional[dict]) -> bool:
    return reply is not None and (reply.get("error") or {}).get("code") == ENGINE_SKEW_CODE


def _poison_tree(engine_root: Path) -> int:
    """Replace every module under the tree with one that raises on import. Returns files replaced."""
    count = 0
    for path in list((engine_root / "coordinator_core").rglob("*.py")):
        staged = path.with_name(path.name + ".swap")
        staged.write_text('raise RuntimeError("tree replaced mid-run")\n', encoding="utf-8")
        os.replace(staged, path)
        count += 1
    return count


def main(argv: Optional[list[str]] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--arm", choices=("production", "model"), default="production")
    parser.add_argument("--copy-secs", type=float, default=DEFAULT_COPY_SECS)
    parser.add_argument("--boot-secs", type=float, default=DEFAULT_BOOT_SECS)
    parser.add_argument(
        "--respawn", choices=(RESPAWN_POST_RELEASE, RESPAWN_PRE_RELEASE), default=RESPAWN_POST_RELEASE
    )
    parser.add_argument("--hook-path", choices=(HOOK_PATH_BASE, HOOK_PATH_BOUNDED), default=HOOK_PATH_BASE)
    parser.add_argument("--spike", action="store_true")
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--assert-budget", action="store_true")
    args = parser.parse_args(argv)
    if args.spike:
        spike = run_old_pair_spike(work_dir=args.work_dir)
        print(f"spike_verdict={spike.verdict}")
        print(json.dumps({**asdict(spike), "verdict": spike.verdict}, sort_keys=True))
        return 0
    if args.arm == "model":
        report = run_swap(
            copy_secs=args.copy_secs,
            boot_secs=args.boot_secs,
            respawn=args.respawn,
            hook_path=args.hook_path,
        )
    else:
        report = run_production_swap(
            copy_secs=args.copy_secs, hook_path=args.hook_path, work_dir=args.work_dir
        )
    print(f"max_unanswered_secs={report.max_unanswered_secs}")
    print(f"unevaluated_guard_count={report.unevaluated_guard_count}")
    print(f"slow_answers={report.slow_answers}")
    print(json.dumps(asdict(report), sort_keys=True))
    if args.assert_budget and not report.within_budget():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
