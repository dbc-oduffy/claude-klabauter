"""Submit-path process-time benchmark: the engine-ask submit hook's default `evaluate`, end to end.

Builds a tmp git repo with one sizing, drives `submit_engine_ask.evaluate` (launch stubbed, default
emit) for a sizing submission and a raw-ask submission, and reports process ms (never wall clock),
spawn count and a per-phase split (sizing read, resume route, premint, emit, minted-run scan, the
rest). A phase is a named hook function wrapped for timing; one the hook no longer has is skipped.
`measure()` is the single entry the pin test drives. Figures are in-process CPU: a spawned child's
CPU is not in them, only its spawn count.

`--concurrent-runs N` measures a `{sizing}` refire submitted while N other sizing runs start on threads:
its figure is the submitting thread's CPU plus its worker threads' (`time.thread_time`), with wall ms and
in-flight-lock wait reported apart as diagnostics (never the bar).

`--scenario multi-run-launch` releases three sizing submissions and a `{sizing}` refire together through the hook's
real entry at default arguments; it reports per-submission process ms, spawns and spooled, and per-leg process ms
(the hook's emit-within-budget, write, direct-start and spool legs plus the phases), round 0 (cold) apart.

Run: `python -m coordinator_core.benchmarks.submit_engine_ask_process_budget [--runs N] [--concurrent-runs N] [--scenario multi-run-launch [--no-prewarm]] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git.run import run_git

#: The brightline process-time bar for the submit path (CLAUDE.md § The brightline).
BAR_MS = 500.0
SIZING_REL = "state/sizings/2026-11-01-submit-budget-bench.yaml"
DEFAULT_RUNS = 5
_SESSION_ID = "11111111-2222-3333-4444-555555555555"

#: phase name -> hook function wrapped for timing. Nested phases (an emit that scans) double-count by design;
#: `other_ms` is the evaluate total minus the top-level phases only.
PHASES = {
    "sizing_read": "_load_sizing_doc",
    "resume_route": "_resume_route",
    "overlap_check": "_live_write_overlap_deny",
    "premint": "_premint_marker",
    "emit": "_default_emit",
    "minted_run_scan": "_run_dirs",
}
_TOP_LEVEL = ("sizing_read", "resume_route", "overlap_check", "premint", "emit")
#: Legs below `emit`, outside the hook: phase name -> (module, attribute) wrapped where the caller looks it up.
#: Importing the module is timed apart (`imports_ms`): the first import in a process is the cold cost.
EXTRA_PHASES = {
    "emit_dispatch": ("coordinator_core.ops.dispatch_emit.op", "_dispatch_emit_body"),
    "compose": ("coordinator_core.ops.dispatch_emit.ask_compose", "compose_ask_script"),
    "script_file": ("coordinator_core.ops.dispatch_emit.op", "_emit_script_file"),
    "contract_checks": ("coordinator_core.ops.dispatch_emit.op", "run_checks"),
}
#: Umbrella legs that contain other legs; never named as the largest cold leg.
_UMBRELLA = ("emit", "emit_dispatch", "script_file")


def _git(root: Path, *args: str) -> None:
    res = run_git(["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args])
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={res.returncode}): {res.stderr.strip()}")


def start_sizing_rel(index: int) -> str:
    """The sizing the `index`-th concurrent run start submits."""
    return f"state/sizings/2026-11-01-submit-budget-bench-start-{index}.yaml"


def _sizing_doc(name: str, deliverable_id: str) -> dict:
    return {
        "schema": "sizing-object", "name": name, "intent": "exercise the submit path",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": deliverable_id, "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }


def build_fixture(root: Path, starts: int = 0) -> None:
    """Populate `root` (an empty dir): a git repo with one committed S sizing, plus `starts` sibling
    sizings (the concurrent run starts' own), and nothing else."""
    sizings = {SIZING_REL: _sizing_doc("submit budget bench", "dlv-submit-budget-bench-abc123")}
    for i in range(starts):
        sizings[start_sizing_rel(i)] = _sizing_doc(f"submit budget bench start {i}", f"dlv-submit-budget-bench-start{i}-abc123")
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    for rel, doc in sizings.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed (submit budget bench)")


def _payload(repo: Path, args: dict) -> dict:
    return {
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SESSION_ID,
        "cwd": str(repo), "tool_use_id": "bench-submit-path",
        "tool_input": {"name": "coordinator:engine-ask", "args": args},
    }


SHAPES = {
    "sizing": lambda repo: _payload(repo, {"sizing": SIZING_REL}),
    "ask": lambda repo: _payload(repo, {"ask": "bench: exercise the raw-ask submit path", "source": "relayed"}),
    "ask_em_context": lambda repo: _payload(repo, {
        "ask": "bench: exercise the raw relayed ask with em_context",
        "source": "relayed",
        "em_context": (
            "EM context (not the PM's words):\n"
            "Engine friction: submitting a run, the emit outran the hook's time budget.\n"
            "A hook over budget is a defect: make the submit/emit path finish inside its budget "
            "(process time, not wall clock), with a test pinning the budget.\n"
        ),
    }),
}
#: A `{sizing}` refire of a halted run whose token a landing attempt stranded: the hook mints a fresh run
#: and takes the plain emit, over a spec-dispatch plan of `HALTED_CHUNKS` rows.
HALTED_SHAPE = "halted_refire"
HALTED_CHUNKS = 6
#: One row's body: ~3 KB, the weight of a real plan row.
HALTED_BODY = "".join(f"    Outcome line {n}: the module does one thing and its test pins both sides of the decision.\n" for n in range(32))
HALTED_PLAN_REL = "docs/plans/2026-11-01-submit-budget-bench.md"
HALTED_RUN = "warp-2026-11-01-submit-budget-bench-20261101T000000-abc123"


def seed_halted(repo: Path) -> None:
    """Leave `repo` holding a halted run of `SIZING_REL` with a stranded claim and a multi-row plan, so the
    next `{sizing}` submission is a refire that mints a fresh run (the hook's plain emit)."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

    rows = "".join(
        f"- id: C{i}\n  title: bench row {i}\n  disposition: open\n  writes:\n    - pkg/m{i}.py\n    - pkg/tests/test_m{i}.py\n"
        f"  body: |\n{HALTED_BODY}"
        + (f"  depends_on:\n    - chunk: C{i - 1}\n      gate_kind: output-consumption-runtime\n" if i else "")
        for i in range(HALTED_CHUNKS)
    )
    plan = repo / HALTED_PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n")
    doc = {**_sizing_doc("submit budget bench", "dlv-submit-budget-bench-abc123"), "plan": HALTED_PLAN_REL}
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    write_halt_marker(repo / "state" / "warp" / HALTED_RUN, halted="refusal", sizing_rel=SIZING_REL, plan_rel=HALTED_PLAN_REL)
    script = f"scratch/warp/{HALTED_RUN}.workflow.mjs"
    token = run_token.mint(repo, HALTED_RUN, script, sizing_rel=SIZING_REL)
    if not run_token.redeem(repo, HALTED_RUN, token, script):
        raise RuntimeError("seed_halted: the stranded claim was not taken")


#: shape -> function leaving the fixture in the state the shape's submission starts from.
SEEDS = {HALTED_SHAPE: seed_halted}
SHAPES[HALTED_SHAPE] = lambda repo: _payload(repo, {"sizing": SIZING_REL})
ENVELOPE_OVERRUN = "started after an emit overrun"
ENVELOPE_PREMINT_DEFERRED = "premint-deferred"
ENVELOPE_DEFERRED = "deferred"
ENVELOPE_SYNCHRONOUS = "synchronous"
#: The text of the hook's spool envelope ("Engine unavailable; ask spooled").
SPOOL_MARKER = "ask spooled"


def envelope_branch(reason: str | None) -> str:
    """Which envelope the hook returned, read off its reason text: the overrun start, a premint-deferred
    or unminted-overrun deferred emit (the run's emit happens in the background), else a synchronous emit."""
    text = reason or ""
    if "emit overrun" in text:
        return ENVELOPE_OVERRUN
    if "deferred emit" in text:
        return ENVELOPE_PREMINT_DEFERRED if text.startswith("Engine submission: run ") else ENVELOPE_DEFERRED
    return ENVELOPE_SYNCHRONOUS
#: Record-write primitives the hook path calls: (module, attribute). One call is one durable write.
WRITE_PRIMITIVES = (
    ("coordinator_core.ops.dispatch_emit.run_token", "mint"),
    ("coordinator_core.hooks.submit_engine_ask", "_write_deferred_script"),
    ("coordinator_core.ops.dispatch_emit.ask_fanout", "_stage_text"),
)
_EM_CONTEXT = ("EM context line: constraint, live checkout, land files atomically. " * 30)[:2048]


def shape_payload(repo: Path, shape: str) -> dict:
    """The hook payload of `shape`: a `SHAPES` key, or `asks:<N>` for one N-entry `asks` submission."""
    if shape in SHAPES:
        return SHAPES[shape](repo)
    count = int(shape.removeprefix("asks:"))
    return _payload(repo, {"asks": [
        {"ask": f"bench job {i}", "label": f"job-{i}", "writes": [f"pkg/m{i}.py"], "em_context": _EM_CONTEXT}
        for i in range(count)
    ]})


@contextlib.contextmanager
def _counted_writes(mp: pytest.MonkeyPatch, counts: dict):
    """Wrap each record-write primitive so every call adds one to `counts[attribute]`."""

    def wrap(owner, attr):
        real = getattr(owner, attr, None)
        if real is None:
            return

        def counted(*a, _real=real, _attr=attr, **k):
            counts[_attr] = counts.get(_attr, 0) + 1
            return _real(*a, **k)

        mp.setattr(owner, attr, counted)

    for modname, attr in WRITE_PRIMITIVES:
        wrap(importlib.import_module(modname), attr)
    yield


@contextlib.contextmanager
def _timed_phases(
    mp: pytest.MonkeyPatch, hook, totals: dict, extra: dict | None = None, imports: dict | None = None,
    only_threads: set | None = None, clock=time.process_time,
):
    """Wrap each present phase function (in `hook`, and in `extra` modules) so its `clock` time accumulates
    into `totals[name]`; a module first imported here adds its import process ms to `imports[module]`.
    With `only_threads` (a live set of thread idents), a call on any other thread runs unwrapped and uncounted."""

    def wrap(owner, attr, name):
        real = getattr(owner, attr, None)
        if real is None:
            return

        def timed(*a, _real=real, _name=name, **k):
            if only_threads is not None and threading.get_ident() not in only_threads:
                return _real(*a, **k)
            t0 = clock()
            try:
                return _real(*a, **k)
            finally:
                totals[_name] = totals.get(_name, 0.0) + (clock() - t0) * 1000.0

        mp.setattr(owner, attr, timed)

    for name, attr in PHASES.items():
        wrap(hook, attr, name)
    for name, (modname, attr) in (extra or {}).items():
        first = modname not in sys.modules
        t0 = time.process_time()
        mod = importlib.import_module(modname)
        if first and imports is not None:
            imports[modname] = round((time.process_time() - t0) * 1000.0, 1)
        wrap(mod, attr, name)
    yield


def _call_once(repo: Path, shape: str, counted: bool = False) -> dict:
    """One `evaluate` of `shape` on a fresh run state; returns ms, spawns, phases, decision."""
    from coordinator_core.hooks import submit_engine_ask as hook

    launched: list = []

    def stub_launch(*a, **k):
        launched.append((a, k))
        return repo / "scratch" / "bench-launcher.workflow.mjs"

    if shape in SEEDS:
        SEEDS[shape](repo)
    payload = shape_payload(repo, shape)
    totals: dict = {}
    imports: dict = {}
    writes: dict = {}
    mp = pytest.MonkeyPatch()
    try:
        mp.chdir(repo)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
        mp.delenv("DELIVERABLE_ID", raising=False)
        spawn_mp = pytest.MonkeyPatch()
        with _timed_phases(mp, hook, totals, EXTRA_PHASES, imports), _counted_writes(mp, writes), \
                _count_spawns_attributed(spawn_mp) as spawns:
            t0 = time.process_time()
            out = hook.evaluate(payload, launch=stub_launch)
            total = (time.process_time() - t0) * 1000.0
    finally:
        mp.undo()
    hso = out.get("hookSpecificOutput") or {}
    phases = {name: round(totals.get(name, 0.0), 1) for name in [*PHASES, *EXTRA_PHASES] if name in totals}
    return {
        "process_ms": total + sum(imports.values()),
        "imports_ms": imports,
        "durable_writes": sum(writes.values()),
        "writes_by_primitive": writes,
        "spawns": len(spawns),
        "spawn_argv": [s.argv for s in spawns],
        "phases": phases,
        "other_ms": round(max(0.0, total - sum(totals.get(n, 0.0) for n in _TOP_LEVEL)), 1),
        "decision": hso.get("permissionDecision"),
        "reason": hso.get("permissionDecisionReason"),
        "launch_stub_called": bool(launched),
    }


def _reset(repo: Path) -> None:
    """Drop what the previous call minted so each measured call starts from the committed fixture."""
    _git(repo, "clean", "-fdxq", "--", "state/warp", "scratch", ".coordinator-local")
    _git(repo, "checkout", "-q", "HEAD", "--", str(Path(SIZING_REL).parent))


def measure_shape(repo: Path, shape: str, runs: int = DEFAULT_RUNS) -> dict:
    """Cold call, then the median of `runs` warm calls (spawn-counted), each on a reset fixture."""
    _reset(repo)
    cold = _call_once(repo, shape)
    samples = []
    for _ in range(runs):
        _reset(repo)
        samples.append(_call_once(repo, shape))
    ordered = sorted(samples, key=lambda s: s["process_ms"])
    mid = ordered[len(ordered) // 2]
    phase_names = sorted({n for s in samples for n in s["phases"]}, key=[*PHASES, *EXTRA_PHASES].index)
    legs = {**cold["phases"], **{f"import:{m}": ms for m, ms in cold["imports_ms"].items()}}
    named = {k: v for k, v in legs.items() if k not in _UMBRELLA}
    return {
        "shape": shape,
        "cold_process_ms": round(cold["process_ms"], 1),
        "cold_phases_ms": cold["phases"],
        "cold_imports_ms": cold["imports_ms"],
        "cold_largest_leg": max(named, key=named.get) if named else None,
        "cold_spawns": cold["spawns"],
        "warm_process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
        "process_ms": round(mid["process_ms"], 1),
        "spawns": mid["spawns"],
        "durable_writes": mid["durable_writes"],
        "writes_by_primitive": mid["writes_by_primitive"],
        "spawn_argv": mid["spawn_argv"],
        "phases_ms": {n: round(statistics.median(s["phases"].get(n, 0.0) for s in samples), 1) for n in phase_names},
        "other_ms": round(statistics.median(s["other_ms"] for s in samples), 1),
        "decision": mid["decision"],
        "reason": mid["reason"],
        "envelope": envelope_branch(mid["reason"]),
        "reached_emit_or_launch": bool(mid["launch_stub_called"] or "emit" in mid["phases"] or "premint" in mid["phases"]),
    }


class _TimedLock:
    """Stands in for the hook's in-flight lock: time the measured thread spends blocked acquiring it."""

    def __init__(self, real, family: set, waited_ms: list) -> None:
        self._real, self._family, self._waited = real, family, waited_ms

    def acquire(self, *a, **k):
        if threading.get_ident() not in self._family:
            return self._real.acquire(*a, **k)
        t0 = time.monotonic()
        try:
            return self._real.acquire(*a, **k)
        finally:
            self._waited[0] += (time.monotonic() - t0) * 1000.0

    def release(self) -> None:
        self._real.release()

    def locked(self) -> bool:
        return self._real.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


@contextlib.contextmanager
def _family_cpu(mp: pytest.MonkeyPatch, family: set, cpu_ms: list):
    """Add to `cpu_ms[0]` the thread CPU of every thread a `family` member starts, run to its end (and keep
    `family` current while each runs): the hook does its reads on worker threads, whose CPU is the
    submit's, not the concurrent starts'."""
    real_init, real_run = threading.Thread.__init__, threading.Thread.run

    def init(self, *a, **k):
        self._bench_family = threading.get_ident() in family
        real_init(self, *a, **k)

    def run(self):
        if not getattr(self, "_bench_family", False):
            return real_run(self)
        family.add(threading.get_ident())
        t0 = time.thread_time()
        try:
            return real_run(self)
        finally:
            cpu_ms[0] += (time.thread_time() - t0) * 1000.0
            family.discard(threading.get_ident())

    mp.setattr(threading.Thread, "__init__", init)
    mp.setattr(threading.Thread, "run", run)
    yield


@contextlib.contextmanager
def _thread_spawns(mp: pytest.MonkeyPatch, family: set, recorded: list):
    """Record the argv of each process a `family` thread spawns."""
    real_popen = subprocess.Popen

    class _Popen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            if threading.get_ident() in family:
                recorded.append(tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),))
            super().__init__(args, *a, **kw)

    mp.setattr(subprocess, "Popen", _Popen)
    yield


def _call_concurrent(repo: Path, concurrent_runs: int) -> dict:
    """One `{sizing}` refire submit issued while `concurrent_runs` other sizing runs start on threads.

    The submitted sizing has no live run (a refire of a live run is denied in microseconds and exercises no
    emit); the starts and the submit release together from one barrier. Its figure is the submitting thread's CPU plus its worker threads'
    (`time.thread_time`), so the concurrent starts do not inflate it; wall ms and lock wait are diagnostics."""
    from coordinator_core.hooks import submit_engine_ask as hook

    def stub_launch(*a, **k):
        return repo / "scratch" / "bench-launcher.workflow.mjs"

    def evaluate(rel: str, tool_use_id: str) -> dict:
        payload = _payload(repo, {"sizing": rel})
        payload["tool_use_id"] = tool_use_id
        return hook.evaluate(payload, launch=stub_launch)

    totals: dict = {}
    imports: dict = {}
    spawned: list = []
    lock_wait = [0.0]
    worker_cpu = [0.0]
    mp = pytest.MonkeyPatch()
    try:
        mp.chdir(repo)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.delenv("DELIVERABLE_ID", raising=False)
        barrier = threading.Barrier(concurrent_runs + 1)
        finished: list = []
        start_out: dict = {}
        result: dict = {}

        def start(i: int) -> None:
            barrier.wait()
            start_out[i] = evaluate(start_sizing_rel(i), f"bench-start-{i}")
            finished.append(i)

        def submit() -> None:
            family = {threading.get_ident()}
            with _timed_phases(mp, hook, totals, EXTRA_PHASES, imports, only_threads=family, clock=time.thread_time), \
                    _family_cpu(mp, family, worker_cpu), _thread_spawns(mp, family, spawned):
                mp.setattr(hook, "_INFLIGHT_LOCK", _TimedLock(hook._INFLIGHT_LOCK, family, lock_wait))
                barrier.wait()
                w0, c0 = time.monotonic(), time.thread_time()
                result["out"] = hook.evaluate(_refire_payload(repo), launch=stub_launch)
                result["thread_ms"] = (time.thread_time() - c0) * 1000.0
                result["wall_ms"] = (time.monotonic() - w0) * 1000.0
                result["unfinished_starts"] = concurrent_runs - len(finished)

        threads = [threading.Thread(target=start, args=(i,)) for i in range(concurrent_runs)]
        threads.append(threading.Thread(target=submit))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        mp.undo()
    hso = result["out"].get("hookSpecificOutput") or {}
    reason = hso.get("permissionDecisionReason")
    phases = {name: round(totals.get(name, 0.0), 1) for name in [*PHASES, *EXTRA_PHASES] if name in totals}
    return {
        "process_ms": result["thread_ms"] + worker_cpu[0] + sum(imports.values()),
        "thread_ms": round(result["thread_ms"], 1),
        "worker_ms": round(worker_cpu[0], 1),
        "wall_ms": round(result["wall_ms"], 1),
        "lock_wait_ms": round(lock_wait[0], 1),
        "unfinished_starts": result["unfinished_starts"],
        "start_decisions": [(start_out.get(i, {}).get("hookSpecificOutput") or {}).get("permissionDecision") for i in range(concurrent_runs)],
        "imports_ms": imports,
        "spawns": len(spawned),
        "spawn_argv": spawned,
        "phases": phases,
        "other_ms": round(max(0.0, result["thread_ms"] - sum(totals.get(n, 0.0) for n in _TOP_LEVEL)), 1),
        "decision": hso.get("permissionDecision"),
        "reason": reason,
        "spooled": SPOOL_MARKER in (reason or ""),
    }


def _refire_payload(repo: Path) -> dict:
    payload = _payload(repo, {"sizing": SIZING_REL})
    payload["tool_use_id"] = "bench-submit-refire"
    return payload


def measure_concurrent(repo: Path, concurrent_runs: int, runs: int = DEFAULT_RUNS) -> dict:
    """Cold call, then `runs` warm calls of `_call_concurrent`, each on a reset fixture; the bar reads the max."""
    _reset(repo)
    cold = _call_concurrent(repo, concurrent_runs)
    samples = []
    for _ in range(runs):
        _reset(repo)
        samples.append(_call_concurrent(repo, concurrent_runs))
    every = [cold, *samples]
    worst = max(every, key=lambda s: s["process_ms"])
    named = {k: v for k, v in {**worst["phases"], **{f"import:{m}": ms for m, ms in worst["imports_ms"].items()}}.items()
             if k not in _UMBRELLA}
    return {
        "shape": f"concurrent:{concurrent_runs}",
        "concurrent_runs": concurrent_runs,
        "cold_process_ms": round(cold["process_ms"], 1),
        "warm_process_ms": round(statistics.median(s["process_ms"] for s in samples), 1) if samples else None,
        "max_process_ms": round(worst["process_ms"], 1),
        "process_ms": round(worst["process_ms"], 1),
        "thread_ms": worst["thread_ms"],
        "worker_ms": worst["worker_ms"],
        "wall_ms": worst["wall_ms"],
        "lock_wait_ms": max(s["lock_wait_ms"] for s in every),
        "wall_ms_max": max(s["wall_ms"] for s in every),
        "unfinished_starts_min": min(s["unfinished_starts"] for s in every),
        "spawns": worst["spawns"],
        "spawn_argv": worst["spawn_argv"],
        "phases_ms": worst["phases"],
        "other_ms": worst["other_ms"],
        "largest_phase": max(named, key=named.get) if named else None,
        "decision": worst["decision"],
        "reason": worst["reason"],
        "envelope": envelope_branch(worst["reason"]),
        "spooled": any(s["spooled"] for s in every),
        "start_decisions": worst["start_decisions"],
    }


SCENARIO_MULTI_RUN = "multi-run-launch"
LAUNCH_STARTS = 3
#: A leg at or over this is named in the report; the bar a leg may not pass is `BAR_MS`.
LEG_NAMED_MS = 200.0
#: Hook legs the multi-run-launch scenario attributes process time to: leg name -> hook function.
LAUNCH_LEGS = {
    "emit_within_budget": "_emit_within_budget",
    "write_leg": "_write_leg",
    "direct_start": "_direct_start",
    "spool_envelope": "_spool_envelope",
}
#: Thread CPU of a worker thread spawned outside every named leg.
_OUTSIDE_LEGS = "threads_outside_legs"
#: Legs that contain other legs: their figure includes the inner ones.
_LAUNCH_UMBRELLA = ("emit", "emit_dispatch", "script_file", "emit_within_budget")


class _Ledger:
    """Attributes thread CPU, leg time and spawns to the submission whose thread family did the work.

    A worker thread started by a family member joins its family; its CPU goes to the innermost leg active
    on its creator when it was created."""

    def __init__(self, count: int) -> None:
        self.lock = threading.Lock()
        self.owner: dict[int, int] = {}
        self.stack: dict[int, list[str]] = {}
        self.base: dict[int, str] = {}
        self.legs: list[dict[str, float]] = [{} for _ in range(count)]
        self.family_ms = [0.0] * count
        self.spawns: list[list[tuple]] = [[] for _ in range(count)]

    def add(self, idx: int, leg: str, ms: float) -> None:
        with self.lock:
            self.legs[idx][leg] = self.legs[idx].get(leg, 0.0) + ms

    def label(self, ident: int) -> str:
        stack = self.stack.get(ident)
        return stack[-1] if stack else self.base.get(ident, _OUTSIDE_LEGS)


@contextlib.contextmanager
def _attributed(mp: pytest.MonkeyPatch, hook, ledger: _Ledger, imports: dict):
    """Wrap the hook's phase and launch legs and every extra-phase module function so a call on a family thread
    adds its thread CPU to `ledger.legs`; patch thread start and `Popen` so worker CPU and spawns are
    credited to the submission that caused them. A module first imported here adds its process ms to `imports`."""

    def wrap(owner, attr, name):
        real = getattr(owner, attr, None)
        if real is None:
            return

        def timed(*a, _real=real, _name=name, **k):
            ident = threading.get_ident()
            idx = ledger.owner.get(ident)
            if idx is None:
                return _real(*a, **k)
            stack = ledger.stack.setdefault(ident, [])
            stack.append(_name)
            t0 = time.thread_time()
            try:
                return _real(*a, **k)
            finally:
                ledger.add(idx, _name, (time.thread_time() - t0) * 1000.0)
                stack.pop()

        mp.setattr(owner, attr, timed)

    for name, attr in {**PHASES, **LAUNCH_LEGS}.items():
        wrap(hook, attr, name)
    for name, (modname, attr) in EXTRA_PHASES.items():
        first = modname not in sys.modules
        t0 = time.process_time()
        mod = importlib.import_module(modname)
        if first:
            imports[modname] = round((time.process_time() - t0) * 1000.0, 1)
        wrap(mod, attr, name)

    real_init, real_run = threading.Thread.__init__, threading.Thread.run

    def init(self, *a, **k):
        ident = threading.get_ident()
        self._bench_parent = (ledger.owner[ident], ledger.label(ident)) if ident in ledger.owner else None
        real_init(self, *a, **k)

    def run(self):
        parent = getattr(self, "_bench_parent", None)
        if parent is None:
            return real_run(self)
        idx, label = parent
        ident = threading.get_ident()
        ledger.owner[ident], ledger.base[ident] = idx, label
        t0 = time.thread_time()
        try:
            return real_run(self)
        finally:
            ms = (time.thread_time() - t0) * 1000.0
            with ledger.lock:
                ledger.family_ms[idx] += ms
            ledger.add(idx, label, ms)
            ledger.owner.pop(ident, None)
            ledger.base.pop(ident, None)
            ledger.stack.pop(ident, None)

    real_popen = subprocess.Popen

    class _Popen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            idx = ledger.owner.get(threading.get_ident())
            if idx is not None:
                ledger.spawns[idx].append(tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),))
            super().__init__(args, *a, **kw)

    mp.setattr(threading.Thread, "__init__", init)
    mp.setattr(threading.Thread, "run", run)
    mp.setattr(subprocess, "Popen", _Popen)
    yield


def _launch_round(repo: Path, starts: int, first: bool) -> dict:
    """One burst: `starts` sibling-sizing submissions and a `{sizing}` refire released together through the
    hook's real entry at default arguments (launch stubbed). Each submission's `process_ms` is its own thread
    CPU plus its worker threads'; a first-in-process round adds the module import cost to every submission,
    as each real hook invocation pays it."""
    from coordinator_core.hooks import submit_engine_ask as hook

    names = [f"start-{i}" for i in range(starts)] + ["refire"]
    ledger = _Ledger(len(names))
    imports: dict = {}
    results: list = [None] * len(names)

    def stub_launch(*a, **k):
        return repo / "scratch" / "bench-launcher.workflow.mjs"

    mp = pytest.MonkeyPatch()
    try:
        mp.chdir(repo)
        mp.setenv("COORDINATOR_SESSION_ID", _SESSION_ID)
        mp.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
        mp.delenv("DELIVERABLE_ID", raising=False)
        with _attributed(mp, hook, ledger, imports):
            barrier = threading.Barrier(len(names))

            def submit(i: int) -> None:
                ident = threading.get_ident()
                ledger.owner[ident] = i
                try:
                    payload = _refire_payload(repo) if names[i] == "refire" else _payload(repo, {"sizing": start_sizing_rel(i)})
                    payload["tool_use_id"] = f"bench-{names[i]}"
                    barrier.wait(timeout=60)
                    w0, c0 = time.monotonic(), time.thread_time()
                    out = hook.evaluate(payload, launch=stub_launch)
                    results[i] = {"out": out, "main_ms": (time.thread_time() - c0) * 1000.0, "wall_ms": (time.monotonic() - w0) * 1000.0}
                except BaseException as exc:  # reported, never raised into the barrier's peers
                    results[i] = {"error": repr(exc)}
                finally:
                    ledger.owner.pop(ident, None)

            threads = [threading.Thread(target=submit, args=(i,)) for i in range(len(names))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    finally:
        mp.undo()
    import_ms = sum(imports.values()) if first else 0.0
    subs = []
    for i, name in enumerate(names):
        res = results[i] or {"error": "no result"}
        hso = (res.get("out") or {}).get("hookSpecificOutput") or {}
        reason = hso.get("permissionDecisionReason")
        subs.append({
            "name": name,
            "error": res.get("error"),
            "process_ms": round(res.get("main_ms", 0.0) + ledger.family_ms[i] + import_ms, 1),
            "wall_ms": round(res.get("wall_ms", 0.0), 1),
            "spawns": len(ledger.spawns[i]),
            "spawn_argv": ledger.spawns[i],
            "spooled": SPOOL_MARKER in (reason or "") or "spooled as" in (reason or ""),
            "decision": hso.get("permissionDecision"),
            "reason": reason,
            "legs_ms": {k: round(v, 1) for k, v in sorted(ledger.legs[i].items(), key=lambda kv: -kv[1])},
        })
    return {"first": first, "imports_ms": imports, "submissions": subs}


def _prewarm_like_the_engine() -> float:
    """Import the emit chain the way a warm pool worker does (`warm.server._prewarm_emit_chain`) and return its
    process ms: a hook runs in that worker, so the chain is already resident when a submission arrives."""
    from coordinator_core.warm import server as warm_server

    t0 = time.process_time()
    warm_server._prewarm_emit_chain()
    return round((time.process_time() - t0) * 1000.0, 1)


def measure_multi_run_launch(repo: Path, starts: int = LAUNCH_STARTS, runs: int = DEFAULT_RUNS, prewarm: bool = True) -> dict:
    """Round 0 (the first burst in this process: caches cold, the emit chain prewarmed as the engine does unless
    `prewarm` is False, which charges every submission the fresh-process import cost), then `runs` warm rounds, each on a
    reset fixture. The leg profile is the per-leg maximum over rounds; a leg at or over `LEG_NAMED_MS` is
    named in `legs_over_200ms`, one at or over `BAR_MS` in `legs_over_bar`."""
    prewarm_ms = _prewarm_like_the_engine() if prewarm else None
    _reset(repo)
    rounds = [_launch_round(repo, starts, first=True)]
    for _ in range(runs):
        _reset(repo)
        rounds.append(_launch_round(repo, starts, first=False))
    legs: dict[str, float] = {}
    leg_round: dict[str, int] = {}
    for n, rnd in enumerate(rounds):
        for sub in rnd["submissions"]:
            for leg, ms in sub["legs_ms"].items():
                if ms > legs.get(leg, -1.0):
                    legs[leg], leg_round[leg] = ms, n
        for mod, ms in rnd["imports_ms"].items():
            legs[f"import:{mod}"], leg_round[f"import:{mod}"] = ms, n
    named = {k: v for k, v in sorted(legs.items(), key=lambda kv: -kv[1]) if k not in _LAUNCH_UMBRELLA}
    every = [s for rnd in rounds for s in rnd["submissions"]]
    worst = max(every, key=lambda s: s["process_ms"])
    return {
        "scenario": SCENARIO_MULTI_RUN,
        "starts": starts,
        "rounds": len(rounds),
        "prewarmed": prewarm,
        "prewarm_ms": prewarm_ms,
        "round0_process_ms": [s["process_ms"] for s in rounds[0]["submissions"]],
        "warm_process_ms": [s["process_ms"] for rnd in rounds[1:] for s in rnd["submissions"]],
        "max_process_ms": worst["process_ms"],
        "process_ms": worst["process_ms"],
        "spawns_per_submission": [s["spawns"] for s in rounds[0]["submissions"]],
        "spawns": sum(s["spawns"] for s in rounds[0]["submissions"]),
        "spawn_argv": [argv for s in rounds[0]["submissions"] for argv in s["spawn_argv"]],
        "spooled": any(s["spooled"] for s in every),
        "decisions": [s["decision"] for s in rounds[0]["submissions"]],
        "errors": [s["error"] for s in every if s["error"]],
        "legs_ms": {k: round(v, 1) for k, v in legs.items()},
        "legs_over_200ms": {k: {"ms": round(v, 1), "round": leg_round[k]} for k, v in named.items() if v >= LEG_NAMED_MS},
        "legs_over_bar": [k for k, v in named.items() if v >= BAR_MS],
        "largest_leg": next(iter(named), None),
        "round_reports": rounds,
        "cold_process_ms": max(s["process_ms"] for s in rounds[0]["submissions"]),
    }


def measure(runs: int = DEFAULT_RUNS, asks: int | None = None, concurrent_runs: int | None = None, scenario: str | None = None, prewarm: bool = True) -> dict:
    """Build the fixture and measure every submit shape (or, with `asks`, only one `asks`-entry submission;
    with `concurrent_runs`, only a `{sizing}` refire submitted while that many runs start; with `scenario`
    `multi-run-launch`, three sizing submissions plus a refire in one burst); returns `{shape: report}`."""
    shapes = [f"asks:{asks}"] if asks else list(SHAPES)
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="submit-budget-bench-")
    try:
        root = scratch / "repo"
        root.mkdir()
        build_fixture(root, starts=LAUNCH_STARTS if scenario else (concurrent_runs or 0))
        with fixture_settings_home(scratch):
            if scenario:
                if scenario != SCENARIO_MULTI_RUN:
                    raise ValueError(f"unknown scenario {scenario!r}; known: {SCENARIO_MULTI_RUN}")
                return {SCENARIO_MULTI_RUN: measure_multi_run_launch(root, LAUNCH_STARTS, runs, prewarm)}
            if concurrent_runs:
                return {f"concurrent:{concurrent_runs}": measure_concurrent(root, concurrent_runs, runs)}
            return {shape: measure_shape(root, shape, runs) for shape in shapes}
    finally:
        rmtree_or_raise(scratch, label="submit-budget-bench")



def _print_multi_run_launch(res: dict, enforce: bool) -> int:
    print(f"== {SCENARIO_MULTI_RUN} ({res['starts']} starts + refire, {res['rounds']} rounds; round 0 is the first burst; "
          f"emit chain {'prewarmed as the engine does, prewarm_ms=' + str(res['prewarm_ms']) if res['prewarmed'] else 'NOT prewarmed (fresh process)'})")
    for n, rnd in enumerate(res["round_reports"]):
        print(f"-- round {n}{' (cold)' if rnd['first'] else ''} imports_ms={rnd['imports_ms']}")
        for sub in rnd["submissions"]:
            print(f"{sub['name']}: process_ms={sub['process_ms']} spawns={sub['spawns']} spooled={sub['spooled']} "
                  f"decision={sub['decision']} wall_ms={sub['wall_ms']} error={sub['error']}")
            print(f"  legs_ms: {sub['legs_ms']}")
    for argv in res["spawn_argv"]:
        print(f"spawn: {' '.join(argv)[:160]}")
    print(f"legs_over_200ms: {res['legs_over_200ms']}")
    print(f"legs_over_bar: {res['legs_over_bar']}")
    print(f"largest_leg: {res['largest_leg']}")
    print(f"spawns_per_submission(round 0): {res['spawns_per_submission']} total={res['spawns']}")
    over = res["max_process_ms"] >= BAR_MS or res["spooled"] or bool(res["legs_over_bar"]) or bool(res["errors"])
    print(f"bar: {'OVER' if over else 'UNDER'} {BAR_MS:.0f}ms process time (max submission {res['max_process_ms']}, round 0 {res['round0_process_ms']})")
    return 2 if enforce and over else 0


def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--asks", type=int, default=None, help="measure one N-ask submission instead of the sizing and ask shapes")
    ap.add_argument("--concurrent-runs", type=int, default=None, help="measure a {sizing} refire submitted while N sizing runs start")
    ap.add_argument("--scenario", choices=[SCENARIO_MULTI_RUN], default=None,
                    help="multi-run-launch: three sizing submissions plus a refire in one burst, process ms and spawns per submission and per leg")
    ap.add_argument("--no-prewarm", action="store_true", help="multi-run-launch: skip the engine's emit-chain prewarm, so round 0 pays the fresh-process imports")
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when any shape's cold or warm process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    report = measure(args.runs, args.asks, args.concurrent_runs, args.scenario, not args.no_prewarm)
    over = False
    for shape, res in report.items():
        if shape == SCENARIO_MULTI_RUN:
            return _print_multi_run_launch(res, args.enforce_bar)
    for shape, res in report.items():
        print(f"== {shape}")
        for key, value in res.items():
            if key != "spawn_argv":
                print(f"{key}: {value}")
        for argv in res["spawn_argv"]:
            print(f"spawn: {' '.join(argv)[:160]}")
        shape_over = max(res["warm_process_ms"] or 0.0, res["cold_process_ms"]) >= BAR_MS or bool(res.get("spooled"))  # warm is None with --runs 0: only cold is measured
        over = over or shape_over
        print(f"bar: {'OVER' if shape_over else 'UNDER'} {BAR_MS:.0f}ms process time (cold {res['cold_process_ms']}, warm {res['warm_process_ms']})")
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
