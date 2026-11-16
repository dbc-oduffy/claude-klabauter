"""Submit-shaped swap harness: door requests for `hooks.submit_engine_ask` issued back to back across
one production publish handover, each classified landed / refused / silent_drop.

Built on `swap_continuous_caller`: the same fixture engine root and warm pair in a throwaway runtime
base, the same `lw.production_handover` swap (a changed engine stamp), the same `PhaseClock` and
process-per-caller isolation. The request is the live submit path: the native door installed under the
`hook-run` entrypoint name (the image the PreToolUse wiring runs), spawned in hook mode with the op
`hooks.submit_engine_ask` and a `Workflow` `coordinator:engine-ask` payload on stdin, from a fixture git
repo holding fixture sizings. The fixture root carries `coordinator/bin/hook-run.py`, the CLI the door's
entrypoint leg runs.

Verdicts (`submit_ask_hook_to_reply_legs.classify_outcome` decides the answer): `landed` is a reply
naming the run it started; `refused` is a deny with a remedy or a spooled ask; `silent_drop` is anything
else (empty stdout, a bare advisory, the door's fail-open text) or a door wall time at or past the
harness hook timeout, which the harness would have killed. The hook timeout is
`submit_engine_ask._CALLER_BUDGET_S` unless `COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S` overrides it; an
unreadable one is a refusal naming the key. `max_process_ms` is the largest spawned-door CPU time
(process time, the DR-344 axis); wall is a drop verdict only against the hook timeout.

Run: `python -m coordinator_core.benchmarks.swap_submit_caller [--duration-s N] [--assert-budget]
[--work-dir <absolute dir>] [--door <image>]`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
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
from coordinator_core.benchmarks import swap_continuous_caller as scc
from coordinator_core.benchmarks import warm_pair_boot_process_time as boot
from coordinator_core.benchmarks.submit_ask_hook_to_reply_legs import ENTRYPOINT, OP, _text_of, classify_outcome
from coordinator_core.benchmarks.submit_engine_ask_process_budget import _payload, build_fixture, start_sizing_rel
from coordinator_core.temp_layout import make_managed_temp_dir

HOOK_TIMEOUT_ENV = "COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S"
HOOK_TIMEOUT_KEY = "submit_engine_ask._CALLER_BUDGET_S"
#: How far past the hook timeout the harness lets a door run before it kills it and records the drop.
KILL_GRACE_SECS = 2.0
#: Fixture sizings the submissions cycle through: a first use starts a run, a repeat is a refire.
SIZING_POOL = 8
LANDED = "landed"
REFUSED = "refused"
SILENT_DROP = "silent_drop"
_OUTCOME = {"minted": LANDED, "refused": REFUSED, "spooled": REFUSED, "silent": SILENT_DROP}
PRE_SWAP = "pre_swap"


class HookTimeoutUnreadable(RuntimeError):
    """The harness hook timeout could not be read; the message names the missing key."""


def hook_timeout_s() -> float:
    """Seconds the harness gives the submit hook before killing it: the env override when it parses
    to a positive number, else the claude-klabauter-side twin of the DoE `_CHILD_BUDGET_S`."""
    try:
        override = float(os.environ.get(HOOK_TIMEOUT_ENV, ""))
    except ValueError:
        override = 0.0
    if override > 0:
        return override
    from coordinator_core.hooks import submit_engine_ask as hook

    value = getattr(hook, "_CALLER_BUDGET_S", None)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not value > 0:
        raise HookTimeoutUnreadable(
            f"the harness hook timeout is unreadable: missing key {HOOK_TIMEOUT_KEY} "
            f"(and {HOOK_TIMEOUT_ENV} is unset or not a positive number)"
        )
    return float(value)


@dataclass(frozen=True)
class Submit:
    """One door request: when it was sent, its wall and process time, its verdict and the text behind it."""

    send_epoch: float
    wall_ms: float
    process_ms: float
    outcome: str
    text: str = ""


def classify(stdout: bytes, returncode: int, wall_ms: float, timeout_ms: float, stderr: bytes = b"") -> "tuple[str, str]":
    """(outcome, text) for one finished door: see the module docstring for the verdicts."""
    if wall_ms >= timeout_ms:
        return SILENT_DROP, f"door ran {wall_ms:.0f}ms, past the {timeout_ms:.0f}ms hook timeout"
    raw = stdout.decode("utf-8", errors="replace").strip()
    if returncode == 2 and stderr.strip():
        return REFUSED, stderr.decode("utf-8", errors="replace").strip()
    try:
        text = _text_of(json.loads(raw)) if raw else ""
    except ValueError:
        text = raw
    outcome = _OUTCOME[classify_outcome(raw)]
    if outcome == SILENT_DROP and not text:
        text = f"door exited {returncode} with no hook answer: {stderr[:200]!r}"
    return outcome, text


@dataclass(frozen=True)
class SubmitReport:
    """The caller's records, the swap's phase epochs (`scc.PHASES` plus `old_pair_stop_done`) and the
    hook timeout the verdicts were measured against."""

    requests: tuple
    hook_timeout_s: float
    phases: dict = field(default_factory=dict)
    successor_answered_before_release: Optional[bool] = None

    def _count(self, outcome: str) -> int:
        return sum(1 for r in self.requests if r.outcome == outcome)

    @property
    def submits(self) -> int:
        return len(self.requests)

    @property
    def landed(self) -> int:
        return self._count(LANDED)

    @property
    def refused(self) -> int:
        return self._count(REFUSED)

    @property
    def silent_drop(self) -> int:
        return self._count(SILENT_DROP)

    @property
    def max_door_wall_ms(self) -> float:
        return round(max((r.wall_ms for r in self.requests), default=0.0), 1)

    @property
    def max_process_ms(self) -> float:
        return round(max((r.process_ms for r in self.requests), default=0.0), 1)

    def within_budget(self) -> bool:
        return (
            self.silent_drop == 0
            and self.max_door_wall_ms < self.hook_timeout_s * 1000.0
            and self.max_process_ms < scc.PROCESS_BUDGET_MS
        )

    def falsifier_line(self) -> str:
        return (
            f"swap_submit_caller: submits={self.submits} landed={self.landed} refused={self.refused} "
            f"silent_drop={self.silent_drop} max_door_wall_ms={self.max_door_wall_ms:g} "
            f"max_process_ms={self.max_process_ms:g} hook_timeout_ms={self.hook_timeout_s * 1000.0:g} "
            f"successor_answered_before_release={self.successor_answered_before_release}"
        )

    def timeline_lines(self) -> list:
        return scc.SwapReport(requests=(), phases=self.phases).timeline_lines()

    def phase_of(self, send_epoch: float) -> str:
        """The last swap phase begun at `send_epoch`, `PRE_SWAP` before the marker take."""
        begun = [(self.phases[n], n) for n in scc.PHASES if n in self.phases and self.phases[n] <= send_epoch]
        return max(begun)[1] if begun else PRE_SWAP

    def phase_lines(self) -> list:
        """One line per phase a submit was sent in: its submits, silent drops and slowest door wall."""
        groups: dict = {}
        for r in self.requests:
            groups.setdefault(self.phase_of(r.send_epoch), []).append(r)
        lines = []
        for name in (PRE_SWAP, *scc.PHASES):
            rows = groups.get(name)
            if rows:
                lines.append(
                    f"  sent in {name:<17} submits={len(rows)} "
                    f"silent_drop={sum(1 for r in rows if r.outcome == SILENT_DROP)} "
                    f"max_door_wall_ms={max(r.wall_ms for r in rows):.1f}"
                )
        return lines

    def slowest_line(self) -> str:
        if not self.requests:
            return "  slowest submit: n/a"
        slow = max(self.requests, key=lambda r: r.wall_ms)
        origin = self.phases.get("marker_take")
        at = f"+{(slow.send_epoch - origin) * 1000.0:.1f}ms since marker_take" if origin is not None else "marker_take not reached"
        return f"  slowest submit: {slow.wall_ms:.1f}ms ({slow.outcome}) sent {at} in {self.phase_of(slow.send_epoch)}"

    def text_lines(self) -> list:
        """One line per distinct refused / silent_drop text, with its count."""
        counts: dict = {}
        for r in self.requests:
            if r.outcome != LANDED:
                key = (r.outcome, (r.text or "no text").replace("\n", " "))
                counts[key] = counts.get(key, 0) + 1
        return [f"  {outcome} x{n}: {text[:200]}" for (outcome, text), n in sorted(counts.items(), key=lambda kv: -kv[1])]


def submit_payload(repo: Path, n: int) -> bytes:
    """The PreToolUse `Workflow` payload of submission `n`: the sizing it names cycles through `SIZING_POOL`."""
    payload = _payload(repo, {"sizing": start_sizing_rel(n % SIZING_POOL)})
    payload["tool_use_id"] = f"toolu_swap_submit_{n}"
    return json.dumps(payload).encode("utf-8")


def door_submit_dispatch(root: Path, door: Path, repo: Path, timeout_s: float) -> Callable[[], Submit]:
    """The dispatch that spawns `door` as the submit hook does: one process per request, hook stdin
    mode, op `hooks.submit_engine_ask`, a submission payload on stdin, run from `repo`. A door still
    running `KILL_GRACE_SECS` past the hook timeout is killed and recorded as a drop."""
    from coordinator_core.win_portability import no_console_creationflags

    env = {
        **os.environ,
        "COORDINATOR_DOOR_ENGINE_ROOT": str(root),
        "COORDINATOR_ENGINE_ROOT": str(root),
        "COORDINATOR_AGENT_TYPE_HOST": "coordinator",
        "COORDINATOR_DOOR_STDIN_MODE": "hook",
    }
    counter = iter(range(1, 1 << 30))

    def dispatch() -> Submit:
        send = time.time()
        started = time.monotonic()
        try:
            proc, cpu_ms = scc._run_door(
                [str(door), OP],
                input=submit_payload(repo, next(counter)),
                env=env,
                cwd=str(repo),
                timeout=timeout_s + KILL_GRACE_SECS,
                **no_console_creationflags(),
            )
        except subprocess.TimeoutExpired:
            wall_ms = (time.monotonic() - started) * 1000.0
            return Submit(send, wall_ms, 0.0, SILENT_DROP, f"door still running {wall_ms:.0f}ms after delivery, killed by the harness")
        wall_ms = (time.monotonic() - started) * 1000.0
        outcome, text = classify(proc.stdout, proc.returncode, wall_ms, timeout_s * 1000.0, proc.stderr)
        return Submit(send, wall_ms, cpu_ms, outcome, text)

    return dispatch


def _submit_loop(dispatch: Callable[[], Submit], stop: threading.Event, out: list) -> None:
    while not stop.is_set():
        record = dispatch()
        out.append(record)
        if record.outcome == SILENT_DROP:
            stop.wait(scc.MISS_BACKOFF_SECS)


class ThreadSubmitCaller:
    """The submit loop as a thread of this process: for a stand-in `dispatch` in a unit test."""

    def __init__(self, dispatch: Callable[[], Submit]) -> None:
        self._stop = threading.Event()
        self._records: list = []
        self._thread = threading.Thread(target=_submit_loop, args=(dispatch, self._stop, self._records), daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> list:
        self._stop.set()
        self._thread.join(timeout=lw.BOOT_WAIT_SECS + 2.0)
        return self._records


def _submit_process_main(root: str, door: str, repo: str, timeout_s: float, ready, stop, out) -> None:
    records: list = []
    ready.set()
    _submit_loop(door_submit_dispatch(Path(root), Path(door), Path(repo), timeout_s), stop, records)
    out.put(records)


class SubmitProcessCaller(scc.ProcessCaller):
    """The submit loop as its own process, as a hook is in production (see `scc.ProcessCaller`)."""

    def __init__(self, root: Path, door: Path, repo: Path, timeout_s: float) -> None:
        import multiprocessing

        super().__init__(root, door)
        self._proc = multiprocessing.get_context("spawn").Process(
            target=_submit_process_main,
            args=(str(root), str(door), str(repo), timeout_s, self._ready, self._stop, self._out),
            daemon=True,
        )


def install_door(root: Path, image: Optional[Path]) -> Path:
    """Install the door `image` in `root` under the `hook-run` entrypoint name; the default image is
    the checkout's Windows `door.exe`, and any other platform needs one passed in."""
    if image is None and sys.platform == "win32":
        image = scc._DOOR_DIR / "door.exe"
    if image is None or not image.is_file():
        raise RuntimeError("no door image for this platform: pass --door <path to a built door>")
    installed = root / (ENTRYPOINT + (".exe" if sys.platform == "win32" else ""))
    shutil.copy2(image, installed)
    return installed


def link_hook_entrypoint(source_root: Path, root: Path) -> None:
    """Give the fixture root the CLI the door's entrypoint leg runs (`coordinator/bin/hook-run.py`) and
    the libraries it resolves, linked from the checkout."""
    bin_src = source_root / "coordinator" / "bin"
    bin_dst = root / "coordinator" / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)
    boot._link_or_copy(str(bin_src / f"{ENTRYPOINT}.py"), str(bin_dst / f"{ENTRYPOINT}.py"))
    skip = shutil.ignore_patterns("__pycache__", "tests")
    for rel in (Path("lib"), Path("..") / "lib" / "resolve-claude-klabauter"):
        src = (bin_src / rel).resolve()
        if src.is_dir():
            shutil.copytree(src, (bin_dst / rel).resolve(), ignore=skip, copy_function=boot._link_or_copy, dirs_exist_ok=True)


@contextlib.contextmanager
def fixture_engine(work_dir: Path, source_root: Path) -> Iterator["tuple[Path, Path]"]:
    """(engine root, submission repo): `scc.fixture_pair`'s answering pair plus the hook entrypoint and a
    git repo of fixture sizings beside it, all removed with the fixture tree."""
    build = boot.build_engine_root

    def build_and_pin(dest: Path, *args, **kwargs) -> Path:
        root = build(dest, *args, **kwargs)
        os.environ["COORDINATOR_ENGINE_ROOT"] = str(root)  # the entrypoint leg's engine-root ladder reads it
        return root

    with mock.patch.dict(os.environ), mock.patch.object(boot, "build_engine_root", build_and_pin), scc.fixture_pair(
        work_dir, source_root
    ) as root:
        link_hook_entrypoint(source_root, root)
        repo = root.parent / "repo"
        repo.mkdir()
        build_fixture(repo, starts=SIZING_POOL)
        yield root, repo


def run(
    *,
    duration_s: Optional[float] = None,
    copy_secs: float = lw.DEFAULT_COPY_SECS,
    work_dir: Optional[Path] = None,
    source_root: Optional[Path] = None,
    door: Optional[Path] = None,
    caller_in_process: bool = False,
) -> SubmitReport:
    """One production handover against a real pair in a throwaway engine root, with a back-to-back
    submit caller across it. `caller_in_process` is for a unit test."""
    timeout_s = hook_timeout_s()
    source = source_root if source_root is not None else Path(__file__).resolve().parents[2]
    base = (work_dir if work_dir is not None else make_managed_temp_dir("swap-submit-caller-")).resolve()
    with fixture_engine(base, source) as (root, repo):
        image = install_door(root, door)
        clock = scc.PhaseClock()
        dispatch = door_submit_dispatch(root, image, repo, timeout_s)
        caller = ThreadSubmitCaller(dispatch) if caller_in_process else SubmitProcessCaller(root, image, repo, timeout_s)
        with clock.wrapped(root):
            observed = scc.observe(
                None,
                lambda: lw.production_handover(root, copy_secs=copy_secs),
                clock=clock,
                duration_s=duration_s,
                caller=caller,
            )
    return SubmitReport(
        requests=observed.requests,
        hook_timeout_s=timeout_s,
        phases=observed.phases,
        successor_answered_before_release=observed.successor_answered_before_release,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--duration-s", type=float, default=None)
    parser.add_argument("--copy-secs", type=float, default=lw.DEFAULT_COPY_SECS)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--door", type=Path, default=None)
    parser.add_argument("--assert-budget", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        report = run(duration_s=args.duration_s, copy_secs=args.copy_secs, work_dir=args.work_dir, door=args.door)
    except RuntimeError as exc:
        print(f"swap_submit_caller: refused: {exc}")
        return 2
    print(report.falsifier_line())
    for line in (*report.timeline_lines(), *report.phase_lines(), report.slowest_line(), *report.text_lines()):
        print(line)
    return 1 if args.assert_budget and not report.within_budget() else 0


if __name__ == "__main__":
    sys.exit(main())
