"""Per-leg process-time profile of `dispatch.land_run` with N unlanded sibling runs in flight.

One `dispatch.land_run` of a clean landing at default params, over a fixture whose record dir
carries N fresh sibling runs, each with a minted token, a sizing and a plan spine and no manifest.
Process time is this process plus descendants (`commit_leg_profile._ChildMeter`'s tree figure read
at each leg boundary); wall clock is never recorded. One JSON row per axis:
`{axis, process_time_ms, spawns, spine_parses, attribution}`.

Axes:
  dispatch.land_run                          the whole op; over `BAR_MS` it is the DR-344 kill gate
  land_run.script_read-to-integrity          the `land_run.script_read` leg start to the
                                             `land_run.integrity` leg start (`land_deadline.check`)
  peer_hunk_gate.co_declared_peer_holds      every call of that function inside the op, summed
                                             (`attribution.calls` is the per-call split)
  peer_hunk_gate.sibling_spine               every `_sibling_spines` call inside the op, summed

`spine_parses` counts yaml loads of a sizing or a plan spine body. `--attribute` adds a cProfile
top-20 per span to `attribution`.

    python coordinator_core/benchmarks/land_run_leg_budget_profile.py [--siblings N] [--tracked N]
        [--chain N] [--json] [--attribute]
    python coordinator_core/benchmarks/land_run_leg_budget_profile.py --real-record <run_id> [--halted-scale K] [--cold-parse] [--json]

`--live-siblings N` runs the measured land_run in its own process while N sibling processes land
their own runs (own session, run, plan, path, checkpoint commit) in the same fixture tree at the
same moment (a go-file barrier), so index locks, record-dir scans and HEAD moves are live. The
measured process is a sibling of the others, never their parent: its figures hold its own tree only.
Rows then add one `leg:<name>` row per `land_deadline.check` leg name in the source census
(`census_legs`) or reached: `process_time_ms` and `spawns` run from that leg's check to the next
check (summed over repeat calls); an unreached census leg is `attribution.not_reached`. `--attribute`
then profiles per leg (cProfile top-20 in each leg row), not per `read_to_integrity` span.

    python coordinator_core/benchmarks/land_run_leg_budget_profile.py --live-siblings 6 [--json]

`build_halted_shape_fixture(tmp_path, stack)` builds the d26504 landing shape (7 product `.py` under
`coordinator_core/ops/dispatch_emit` plus a `docs/research` `.md`, peer-dirty paths alongside).

`--real-record` replays a recorded run's land_run params (rebuilt from `run-outcome.json`, the
manifest and `run-base` when `terminal-commit-params.json` is absent) in a throwaway `--shared` clone of the
repo (its `state/warp` and `scratch/warp` record copied in) and stops at the `land_run.commit` leg
boundary by raising `StopBeforeCommit`: nothing is committed, and the live tree is only read.
Rows then carry a per-leg table (`legs`) and `land_deadline_halt`. The code under test is the tree
this file sits in, so copying the file into a `git archive` of an older commit measures that
commit against the same record (`--source-repo` names the live repo for the clone).
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.benchmarks.land_run_slow_disk_profile import (  # noqa: E402
    DEFAULT_CHAIN,
    DEFAULT_TRACKED,
    OFF_CHAIN_BASE,
    OWN_LINES,
    PATH_P,
    PLAN,
    RUN_ID,
    SCRIPT,
    SELF,
    _bind_session,
    _git,
    _call_land,
    _outcome,
    _write,
    build_land_fixture,
)

BAR_MS = 500.0
LEG_BAR_MS = 200.0
DEFAULT_SIBLINGS = 8
WHOLE_OP = "dispatch.land_run"
LEG_SCRIPT_READ = "land_run.script_read"
LEG_INTEGRITY = "land_run.integrity"
LEG_COMMIT = "land_run.commit"
AXIS_READ_TO_INTEGRITY = "land_run.script_read-to-integrity"
AXIS_HOLDS = "peer_hunk_gate.co_declared_peer_holds"
AXIS_SPINE = "peer_hunk_gate.sibling_spine"
SIZING_MARK = "sizing_profile_mark: 1"
LEG_AXIS_PREFIX = "leg:"
RESULT_MARK = "LEG-PROFILE-RESULT "
WORKER_WAIT_SECS = 180.0
CENSUS_FILES = ("land_run.py", "peer_hunk_gate.py", "co_declared_settle.py", "hunk_provenance.py", "landing_integrity.py")
EXCEPTED_LEGS = frozenset({"land_run._integrity.landing_test_gate"})  # the bulk retest: outside the 500ms bar
_CHECK_RE = re.compile(r"land_deadline\.check\(\s*[\"']([a-z_.]+)[\"']\s*\)")


class StopBeforeCommit(BaseException):
    """Raised at the `land_run.commit` leg boundary so a replay never reaches the commit leg
    (BaseException, like `LandDeadlineExceeded`: no `except Exception` in the op swallows it)."""


@dataclass
class AxisRow:
    """One measured axis."""

    axis: str
    process_time_ms: float = 0.0
    spawns: int = 0
    spine_parses: int = 0
    attribution: dict = field(default_factory=dict)


def sibling_run_id(i: int) -> str:
    """A run id whose stamp is minutes old, so it is inside the token TTL and sorts before any later id."""
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=10 + i)).strftime("%Y%m%dT%H%M%S")
    return f"plan-{stamp}-{i:06x}"


def add_siblings(root: Path, count: int) -> list:
    """`count` unlanded sibling runs in `root`: a minted token, a `run.json` naming a sizing, the
    sizing naming a plan, the plan a `## Tasks` spine whose `writes` are the sibling's own
    paths. No manifest, so each is read through its spine. Returns the run ids."""
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    ids = []
    for i in range(count):
        run_id = sibling_run_id(i)
        sizing_rel = f"state/sizings/sib-{i}.yaml"
        plan_rel = f"docs/plans/sib-{i}.md"
        _write(root, sizing_rel, f"{SIZING_MARK}\nplan: {plan_rel}\ndeliverable_id: sib-{i}\n")
        rows = "".join(
            f"- id: S{j}\n  title: sibling {i} row {j}\n  writes:\n    - src/sib{i}/m{j}.py\n    - src/sib{i}/tests/t{j}.py\n"
            for j in range(4)
        )
        _write(root, plan_rel, f"# sibling {i}\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n")
        _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", json.dumps({"run_id": run_id, "sizing_rel": sizing_rel}))
        run_token.mint(root, run_id, f"run-sib-{i}.mjs", session_id=f"{i:08x}-0000-4000-8000-000000000000", sizing_rel=sizing_rel)
        ids.append(run_id)
    return ids


class SpineParseCounter:
    """Counts yaml loads whose text is a sizing (carries `SIZING_MARK`) or a plan spine body (a
    list of `- id:` rows); a load of anything else is not counted."""

    def __init__(self) -> None:
        self.sizing = 0
        self.plan = 0

    @property
    def total(self) -> int:
        return self.sizing + self.plan

    def see(self, stream: object) -> None:
        text = stream if isinstance(stream, str) else (stream.decode("utf-8", "replace") if isinstance(stream, bytes) else "")
        if SIZING_MARK in text:
            self.sizing += 1
        elif text.lstrip().startswith("- id:"):
            self.plan += 1


class Profiler:
    """Marks at every `land_deadline.check`, per-call timing of the two gate functions, and counters."""

    def __init__(self, attribute: bool, stop_before_commit: bool = False, per_leg: bool = False) -> None:
        self.attribute = attribute
        self.stop_before_commit = stop_before_commit
        self.per_leg = per_leg
        self.commit_leg_reached = False
        self.start_ms = 0.0
        self.meter = _ChildMeter()
        self.spawns = 0
        self.parses = SpineParseCounter()
        self.marks: list = []
        self.calls: dict = {AXIS_HOLDS: [], AXIS_SPINE: []}
        self.profiles: dict = {}
        self._active = None

    def tree_ms(self) -> float:
        """Process time of this process plus descendants so far, per OS."""
        win = getattr(self.meter, "_win", None)
        if win is not None:
            return float(win.snapshot()["process_time_ms"])
        import resource

        own = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime) * 1000.0

    def _profile_on(self, span: str) -> None:
        if self.attribute and self._active is None:
            prof = self.profiles.setdefault(span, cProfile.Profile(time.process_time))
            prof.enable()
            self._active = prof

    def _profile_off(self) -> None:
        if self._active is not None:
            self._active.disable()
            self._active = None

    def mark(self, leg: str) -> None:
        self.marks.append((leg, self.tree_ms(), self.spawns))
        if self.per_leg:
            self._profile_off()
            self._profile_on(LEG_AXIS_PREFIX + leg)
        elif leg == LEG_SCRIPT_READ:
            self._profile_on(AXIS_READ_TO_INTEGRITY)
        elif leg == LEG_INTEGRITY:
            self._profile_off()
        if leg == LEG_COMMIT:
            self.commit_leg_reached = True
            if self.stop_before_commit:
                raise StopBeforeCommit(leg)

    def legs(self) -> list:
        """Process ms and spawns between consecutive marks, each labelled by the mark that opens it
        (`start` covers the op start to the first mark; `<leg>` runs to the next mark)."""
        out, prev = [], (None, self.start_ms, 0)
        for leg, ms, spawns in self.marks:
            out.append({"leg": prev[0] or "start", "to": leg, "process_time_ms": round(ms - prev[1], 3),
                        "spawns": spawns - prev[2]})
            prev = (leg, ms, spawns)
        return out

    def leg_costs(self, end_ms: float, end_spawns: int) -> dict:
        """Per leg name, in first-reached order: process ms and spawns from each of its checks to the
        next check (the last to `end_*`), summed over repeat calls, and the call count."""
        costs: dict = {}
        following = self.marks[1:] + [("", end_ms, end_spawns)]
        for (leg, ms, spawns), (_, ms2, spawns2) in zip(self.marks, following):
            c = costs.setdefault(leg, {"process_time_ms": 0.0, "spawns": 0, "calls": 0})
            c["process_time_ms"] = round(c["process_time_ms"] + ms2 - ms, 3)
            c["spawns"] += spawns2 - spawns
            c["calls"] += 1
        return costs

    def timed(self, axis: str, fn):
        def wrapper(*a, **k):
            t0, s0 = self.tree_ms(), self.spawns
            p0 = self.parses.total
            nested = self._active is not None
            if not nested:
                self._profile_on(axis)
            try:
                return fn(*a, **k)
            finally:
                if not nested:
                    self._profile_off()
                self.calls[axis].append(
                    {"process_time_ms": round(self.tree_ms() - t0, 3), "spawns": self.spawns - s0,
                     "spine_parses": self.parses.total - p0}
                )

        return wrapper

    def span(self, start: str, end: str) -> tuple:
        """`(process_ms, spawns)` from the first mark of `start` to the next mark of `end`, or None."""
        for i, (leg, ms, spawns) in enumerate(self.marks):
            if leg != start:
                continue
            for leg2, ms2, spawns2 in self.marks[i + 1:]:
                if leg2 == end:
                    return round(ms2 - ms, 3), spawns2 - spawns
            return None
        return None

    def top_functions(self, span: str) -> list:
        prof = self.profiles.get(span)
        if prof is None:
            return []
        rows = []
        for (fname, _line, name), (_cc, nc, tt, ct, _c) in pstats.Stats(prof).stats.items():
            f = fname.replace("\\", "/")
            if "coordinator_core/" in f and "benchmarks/" not in f:
                rows.append((ct, f"{f.split('coordinator_core/', 1)[1]}:{name}", nc, tt))
        rows.sort(reverse=True)
        return [
            {"cum_ms": round(ct * 1000, 1), "fn": fn, "calls": nc, "self_ms": round(tt * 1000, 1)}
            for ct, fn, nc, tt in rows[:20]
        ]


def _install(profiler: Profiler):
    """Patch the seams; returns the undo callable."""
    import yaml

    from coordinator_core.ops.dispatch_emit import land_deadline, peer_hunk_gate

    spine_name = "_sibling_spines" if hasattr(peer_hunk_gate, "_sibling_spines") else "_spine_writes"
    real_check, real_holds, real_spine = land_deadline.check, peer_hunk_gate.co_declared_peer_holds, getattr(peer_hunk_gate, spine_name)
    real_safe, real_load, real_init = yaml.safe_load, yaml.load, subprocess.Popen.__init__

    def check(leg):
        real_check(leg)
        profiler.mark(leg)

    inside = []  # yaml.safe_load calls yaml.load: one parse, one count

    def safe_load(stream, *a, **k):
        profiler.parses.see(stream)
        inside.append(1)
        try:
            return real_safe(stream, *a, **k)
        finally:
            inside.pop()

    def load(stream, *a, **k):
        if not inside:
            profiler.parses.see(stream)
        return real_load(stream, *a, **k)

    def popen_init(self, *a, **k):
        profiler.spawns += 1
        return real_init(self, *a, **k)

    land_deadline.check = check
    peer_hunk_gate.co_declared_peer_holds = profiler.timed(AXIS_HOLDS, real_holds)
    setattr(peer_hunk_gate, spine_name, profiler.timed(AXIS_SPINE, real_spine))
    yaml.safe_load, yaml.load = safe_load, load
    subprocess.Popen.__init__ = popen_init

    def undo() -> None:
        land_deadline.check = real_check
        peer_hunk_gate.co_declared_peer_holds = real_holds
        setattr(peer_hunk_gate, spine_name, real_spine)
        yaml.safe_load, yaml.load = real_safe, real_load
        subprocess.Popen.__init__ = real_init

    return undo


def census_legs() -> list:
    """Every `land_deadline.check("<leg>")` name in the landing path files (`CENSUS_FILES`), sorted."""
    from coordinator_core.ops.dispatch_emit import land_deadline

    folder = Path(land_deadline.__file__).parent
    names: set = set()
    for name in CENSUS_FILES:
        path = folder / name
        if path.is_file():
            names.update(_CHECK_RE.findall(path.read_text(encoding="utf-8")))
    return sorted(names)


def leg_rows(prof: Profiler, end_ms: float, end_spawns: int) -> list:
    """One `leg:<name>` row per census leg and per reached leg outside it; a census leg the op never
    started is `not_reached` (0 ms, 0 spawns)."""
    costs = prof.leg_costs(end_ms, end_spawns)
    census = census_legs()
    rows = []
    for leg in census + [leg for leg in costs if leg not in census]:
        c = costs.get(leg)
        attribution = {"leg": leg, "in_census": leg in census, "excepted": leg in EXCEPTED_LEGS, "measured": c is not None, "not_reached": c is None,
                       "calls": c["calls"] if c else 0, "top_functions": prof.top_functions(LEG_AXIS_PREFIX + leg)}
        rows.append(AxisRow(LEG_AXIS_PREFIX + leg, c["process_time_ms"] if c else 0.0, c["spawns"] if c else 0, 0, attribution))
    return rows


def profile_land(root: Path, base: str, *, attribute: bool, call=None, stop_before_commit: bool = False,
                 legs: bool = False) -> list:
    """Run `dispatch.land_run` once in `root` and return the axis rows. `call(root, base)` replaces
    the fixture landing; `stop_before_commit` ends the op at the commit leg boundary; `legs` adds
    the `leg:<name>` rows (and moves `--attribute` from the read-to-integrity span to each leg)."""
    prof = Profiler(attribute, stop_before_commit, per_leg=legs)
    undo = _install(prof)
    result, error = None, ""
    prof.meter.start()
    prof.start_ms = prof.tree_ms()
    cpu0 = time.process_time()
    end = (prof.start_ms, 0)
    try:
        result = (call or _call_land)(root, base)
    except StopBeforeCommit:
        error = ""
    except BaseException as exc:  # noqa: BLE001 -- a raised op is a row, not a crash of the harness
        error = type(exc).__name__
    finally:
        end = (prof.tree_ms(), prof.spawns)
        whole_ms = round((time.process_time() - cpu0) * 1000.0 + prof.meter.stop_ms(), 3)
        undo()
        prof._profile_off()
    rows = [AxisRow(
        WHOLE_OP, whole_ms, prof.spawns, prof.parses.total,
        {"outcome": error and f"raised:{error}" or (_outcome(result) if result is not None else "stopped_before_commit"),
         "sizing_parses": prof.parses.sizing, "plan_parses": prof.parses.plan,
         "commit_leg_reached": prof.commit_leg_reached,
         "land_deadline_halt": isinstance(result, dict) and result.get("refused") == "land-deadline",
         "legs": prof.legs(), "error": (result or {}).get("error", "") if isinstance(result, dict) else ""},
    )]
    span = prof.span(LEG_SCRIPT_READ, LEG_INTEGRITY)
    rows.append(AxisRow(
        AXIS_READ_TO_INTEGRITY, span[0] if span else 0.0, span[1] if span else 0, 0,
        {"measured": span is not None, "top_functions": prof.top_functions(AXIS_READ_TO_INTEGRITY)},
    ))
    for axis in (AXIS_HOLDS, AXIS_SPINE):
        calls = prof.calls[axis]
        rows.append(AxisRow(
            axis, round(sum(c["process_time_ms"] for c in calls), 3), sum(c["spawns"] for c in calls),
            sum(c["spine_parses"] for c in calls),
            {"measured": bool(calls), "calls": calls, "top_functions": prof.top_functions(axis)},
        ))
    if legs:
        rows.extend(leg_rows(prof, *end))
    return rows


def kill_gate(rows: list) -> bool:
    """True when the whole-op row is over `BAR_MS`: DR-344 deletes it, nothing downstream starts."""
    return any(r.axis == WHOLE_OP and r.process_time_ms > BAR_MS for r in rows)


def run(workdir: Path, siblings: int, tracked: int, chain: int, attribute: bool) -> list:
    declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        root, base = build_land_fixture(workdir, mode="clean", tracked=tracked, chain=chain, stack=stack)
        add_siblings(root, siblings)
        rows = profile_land(root, base, attribute=attribute)
    for row in rows:
        row.attribution["siblings"] = siblings
    return rows


def live_spec(i: int) -> dict:
    """The identity of live sibling `i`: its own session, run, script, plan and path."""
    return {
        "session": f"{0x1000 + i:08x}-0000-4000-8000-0000000000c0",
        "run_id": f"plan-20261028T1000{i:02d}-c1{i:04x}",
        "script": f"run-live{i}.mjs",
        "plan": f"docs/plans/live-{i}.md",
        "path": f"src/live{i}/p.py",
    }


def add_live_sibling_runs(root: Path, count: int) -> list:
    """For each live sibling: a stamped checkpoint commit adding its own lines to its own path, a
    worktree edit past it, its session scope and touch record, `run.json` and emitted script."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.session import core as session_core
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record

    specs = [live_spec(i) for i in range(count)]
    for spec in specs:
        sess, path, plan = spec["session"], spec["path"], spec["plan"]
        session_core.init(sess, cwd=str(root))
        _write(root, path, "head = 0\n" + OWN_LINES)
        _write(root, plan, "# live plan\n")
        _git(["add", "--", path, plan], root)
        message = (f"checkpoint(wave 0): 1 row - C1\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {OFF_CHAIN_BASE}\n"
                   f"Checkpoint-Not-Landed: run-open\nSession-Id: {sess}\n")
        _git(["commit", "-q", "-m", message, "--", path, plan], root)
        _write(root, path, "head = 0\n" + OWN_LINES + "tail = 1\n")
        session_scope.touch(sess, path, cwd=str(root))
        sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / sess)
        touch_record.append_event(
            sink, session_id=sess, agent_id=None, verb="T", path=path,
            content_hash=touch_record.compute_content_hash(root / path), kind="w",
        )
        _write(root, f"{RECORD_DIR_ROOT}/{spec['run_id']}/run.json", json.dumps({"run_id": spec["run_id"]}))
        request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(path,)),), plan_path=plan)
        _write(root, spec["script"], "// emitted\n" + render_marker(request) + "\n")
    return specs


def _call_live_sibling(root: Path, base: str, spec: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    land_run._launch_close_job = lambda *a, **k: True
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": spec["script"], "incomplete_chunks": [], "inline_review": review,
         "session_id": spec["session"], "run_id": spec["run_id"], "run_base_sha": base},
        repo_root=root / ".git",
    )


def _worker(tag: str, root: Path, base: str, sync: Path, attribute: bool) -> int:
    """A live-run process: import everything, announce `ready-<tag>`, wait for `go`, then land (the
    measured worker `m` profiles its landing and prints its rows, a sibling `s<i>` prints its outcome)."""
    declare_benchmark_origin()
    from coordinator_core.ops.dispatch_emit import land_deadline, land_run, peer_hunk_gate  # noqa: F401
    from coordinator_core.ops.dispatch_emit.tests import _review_fixture  # noqa: F401

    measured = tag == "m"
    spec = None if measured else live_spec(int(tag[1:]))
    if measured:
        _bind_session()
    else:
        os.environ["COORDINATOR_SESSION_ID"] = spec["session"]
    (sync / f"ready-{tag}").write_text("1", encoding="utf-8")
    give_up = time.monotonic() + WORKER_WAIT_SECS
    while not (sync / "go").exists():
        if time.monotonic() > give_up:
            return 3
        time.sleep(0.005)
    if measured:
        payload = [asdict(r) for r in profile_land(root, base, attribute=attribute, legs=True)]
    else:
        try:
            payload = {"outcome": _outcome(_call_live_sibling(root, base, spec))}
        except Exception as exc:  # noqa: BLE001 -- a raised sibling is an outcome, not a crash
            payload = {"outcome": f"raised:{type(exc).__name__}"}
    print(RESULT_MARK + json.dumps(payload))
    return 0


def _spawn_worker(tag: str, root: Path, base: str, sync: Path, attribute: bool) -> subprocess.Popen:
    argv = [sys.executable, str(Path(__file__).resolve()), "--worker", tag, "--worker-root", str(root),
            "--worker-base", base, "--worker-sync", str(sync)] + (["--attribute"] if attribute else [])
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _spawn_workers(tags: list, root: Path, base: str, sync: Path, attribute: bool) -> dict:
    """Launch every worker concurrently from one pool; the simultaneous launch is the measured subject."""
    with ThreadPoolExecutor(max_workers=len(tags)) as pool:
        launched = pool.map(lambda tag: _spawn_worker(tag, root, base, sync, attribute), tags)
        return dict(zip(tags, launched))


def _result_of(tag: str, proc: subprocess.Popen):
    out, err = proc.communicate(timeout=WORKER_WAIT_SECS)
    for line in reversed(out.splitlines()):
        if line.startswith(RESULT_MARK):
            return json.loads(line[len(RESULT_MARK):])
    raise RuntimeError(f"live worker {tag} rc={proc.returncode} printed no result: {err.strip()[-400:]}")


def run_live(workdir: Path, live: int, siblings: int, tracked: int, chain: int, attribute: bool) -> list:
    """The measured landing (its own process) with `live` sibling processes landing their own runs in
    the same tree at the same moment; rows carry the per-leg table and each sibling's outcome."""
    declare_benchmark_origin()
    _bind_session()
    with ExitStack() as stack:
        root, base = build_land_fixture(workdir, mode="clean", tracked=tracked, chain=chain, stack=stack)
        add_siblings(root, siblings)
        add_live_sibling_runs(root, live)
        sync = workdir / "sync"
        sync.mkdir()
        tags = ["m"] + [f"s{i}" for i in range(live)]
        procs = _spawn_workers(tags, root, base, sync, attribute)
        try:
            give_up = time.monotonic() + WORKER_WAIT_SECS
            while not all((sync / f"ready-{tag}").exists() for tag in tags):
                dead = [t for t, p in procs.items() if p.poll() is not None]
                if dead or time.monotonic() > give_up:
                    raise RuntimeError(f"live workers not ready (exited early: {dead}): {_result_stderr(procs, dead)}")
                time.sleep(0.01)
            (sync / "go").write_text("1", encoding="utf-8")
            results = {tag: _result_of(tag, proc) for tag, proc in procs.items()}
        finally:
            for proc in procs.values():
                if proc.poll() is None:
                    proc.kill()
    rows = [AxisRow(**r) for r in results["m"]]
    outcomes = {tag: res["outcome"] for tag, res in results.items() if tag != "m"}
    for row in rows:
        row.attribution["siblings"] = siblings
        row.attribution["live_siblings"] = live
    rows[0].attribution["live_sibling_outcomes"] = outcomes
    return rows


def _result_stderr(procs: dict, tags: list) -> str:
    return "; ".join(procs[t].communicate()[1].strip()[-300:] for t in tags)


HALTED_SHAPE_PY = tuple(f"coordinator_core/ops/dispatch_emit/halted_shape_m{i}.py" for i in range(7))
HALTED_SHAPE_DOC = "docs/research/halted-shape-finding.md"
HALTED_SHAPE_PEER_DIRTY = ("peer/dirty_a.txt", "peer/dirty_b.txt", "tracked/d00/s0/f000000.txt")


def build_halted_shape_fixture(tmp_path: Path, stack: ExitStack, *, tracked: int = 50, chain: int = 10) -> dict:
    """The shape of the halted run `ask-20261030T235004-d26504`, as a clean landing: seven product
    `.py` files under `coordinator_core/ops/dispatch_emit` plus one `docs/research/*.md`, all
    uncommitted and touched by the run's session and named by the emitted script's one chunk, on a
    tree that also carries peer-dirty paths the session never touched. Returns
    `{root, base, run_id, script, paths, peer_dirty}`; land it with `profile_land(root, base,
    attribute=False, legs=True)`."""
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record

    root, base = build_land_fixture(tmp_path, mode="clean", tracked=tracked, chain=chain, stack=stack)
    paths = [*HALTED_SHAPE_PY, HALTED_SHAPE_DOC]
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    for i, rel in enumerate(paths):
        _write(root, rel, f"# halted shape {i}\n" if rel.endswith(".md") else f'"""Halted-shape module {i}."""\n\nVALUE_{i} = {i}\n')
        session_scope.touch(SELF, rel, cwd=str(root))
        touch_record.append_event(
            sink, session_id=SELF, agent_id=None, verb="T", path=rel,
            content_hash=touch_record.compute_content_hash(root / rel), kind="w",
        )
    for rel in HALTED_SHAPE_PEER_DIRTY:
        _write(root, rel, f"peer dirt {rel}\n")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(PATH_P, *paths)),), plan_path=PLAN)
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return {"root": root, "base": base, "run_id": RUN_ID, "script": SCRIPT, "paths": paths, "peer_dirty": list(HALTED_SHAPE_PEER_DIRTY)}


def _json_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def reconstructed_recorded(source_root: Path, run_id: str) -> dict:
    """What `terminal-commit-params.json` would hold, rebuilt from the run record when that file is
    absent: the emitted script path, `doc_writes` from the manifest, and `run-outcome.json`."""
    state = source_root / "state" / "warp" / run_id
    outcome = _json_of(state / "run-outcome.json")
    if not outcome:
        raise FileNotFoundError(f"{run_id}: no terminal-commit-params.json and no state/warp/{run_id}/run-outcome.json to rebuild from")
    return {
        "script_path": f"scratch/warp/{run_id}.workflow.mjs",
        "doc_paths": _json_of(state / "manifest.json").get("doc_writes") or [],
        "run_outcome": outcome,
    }


def record_run_base(source_root: Path, run_id: str) -> str:
    """The run's base sha: `scratch/warp/<run>/run-base`, else `halted.json` `run_base_sha`, else the
    manifest `head_sha`."""
    marker = source_root / "scratch" / "warp" / run_id / "run-base"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip():
        return marker.read_text(encoding="utf-8").strip()
    state = source_root / "state" / "warp" / run_id
    return _json_of(state / "halted.json").get("run_base_sha") or _json_of(state / "manifest.json")["head_sha"]


def real_record_params(source_root: Path, run_id: str) -> dict:
    """The product-mode `dispatch.land_run` params recorded for `run_id` in `scratch/warp/<run>/
    terminal-commit-params.json`, minus the halt envelope and the run token (a replay claims no token).
    A run with no such file is rebuilt by `reconstructed_recorded` and based at `record_run_base`."""
    params_file = source_root / "scratch" / "warp" / run_id / "terminal-commit-params.json"
    if params_file.is_file():
        recorded = json.loads(params_file.read_text(encoding="utf-8"))
        base = json.loads((source_root / "state" / "warp" / run_id / "manifest.json").read_text(encoding="utf-8"))["head_sha"]
    else:
        recorded = reconstructed_recorded(source_root, run_id)
        base = record_run_base(source_root, run_id)
    outcome = recorded["run_outcome"]
    review = dict(outcome["inline_review"])
    review["prep"] = {**(review.get("prep") or {}), "run_base_sha": base}
    return {
        "script_path": recorded["script_path"], "run_id": run_id, "run_base_sha": base,
        "incomplete_chunks": outcome.get("incomplete_chunks") or [], "landed_chunks": outcome.get("landed_chunks") or [],
        "ledger_verdict": outcome.get("ledger_verdict"), "doc_paths": recorded.get("doc_paths") or [],
        "inline_review": review, "session_id": SELF,
    }


def seed_real_record(source_root: Path, clone: Path, run_id: str) -> None:
    """Copy the run's `state/warp` and `scratch/warp` record, the halt-withheld index and its built
    sentinel, and each path the run's `halted.json` withheld that the live worktree still holds (the
    shared dirty tree the run saw: present in the clone, uncommitted)."""
    from coordinator_core.ops.dispatch_emit.halted_withheld_index import INDEX_DIR

    for rel in (f"state/warp/{run_id}", f"scratch/warp/{run_id}", INDEX_DIR):
        if (source_root / rel).is_dir():
            shutil.copytree(source_root / rel, clone / rel, dirs_exist_ok=True)
    for name in (f"{run_id}.workflow.mjs", f"{run_id}.workflow.mjs.emitted.json"):
        src = source_root / "scratch" / "warp" / name
        if src.is_file():
            (clone / "scratch" / "warp").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, clone / "scratch" / "warp" / name)
    withheld = _json_of(source_root / "state" / "warp" / run_id / "halted.json").get("run_artifacts", {}).get("withheld") or {}
    for rel in withheld:
        if (source_root / rel).is_file():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_root / rel, clone / rel)


def enter_record_fixture(stack: ExitStack, source_root: Path, params: dict, *, prefix: str, label: str) -> Path:
    """Enter a `record_fixture_repo` of the run's `doc_paths` (the committed base) on `stack`, point
    `params`' base shas at its commit, and set the committer identity. The record, script and withheld
    paths are then seeded uncommitted by `seed_real_record`, as the run left them."""
    from coordinator_core.benchmarks.isolated_clone import record_fixture_repo

    clone = stack.enter_context(record_fixture_repo(source_root, params.get("doc_paths") or [], prefix=prefix, label=label))
    head = run_git(["rev-parse", "HEAD"], cwd=str(clone))
    if not head.ok:
        raise RuntimeError(f"git rev-parse HEAD failed in {clone}")
    sha = head.stdout.strip()
    params["run_base_sha"] = sha
    params["inline_review"]["prep"]["run_base_sha"] = sha
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        if not run_git(["config", key, value], cwd=str(clone)).ok:
            raise RuntimeError(f"git config {key} failed in {clone}")
    return clone


def in_window_halted_runs(clone: Path) -> list:
    """Run ids under `clone/state/warp` whose `halted.json` is a `halted` marker younger than
    `run_token.TTL_SECONDS` (by the run-id stamp the landing gates read, else the marker's mtime)."""
    from coordinator_core.ops.dispatch_emit import halted_run, run_token, stopped_runs
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    base = clone / RECORD_DIR_ROOT
    if not base.is_dir():
        return []
    now = time.time()
    out = []
    for entry in sorted(os.scandir(base), key=lambda e: e.name):
        marker_file = Path(entry.path) / halted_run.HALTED_FILE
        if not entry.is_dir() or not marker_file.is_file():
            continue
        marker = halted_run._read(Path(entry.path))  # private to the landing gates, as is _stamp_epoch below: rename in lockstep
        if marker is None or marker.get("status") != halted_run.STATUS_HALTED:
            continue
        stamp = stopped_runs._stamp_epoch(entry.name)
        if stamp is None:
            stamp = marker_file.stat().st_mtime
        if now - stamp <= run_token.TTL_SECONDS:
            out.append(entry.name)
    return out


def scale_halted_runs(clone: Path, scale: int) -> int:
    """Copy each in-window halted run `scale - 1` times inside `clone` as `<run>-x<k>` (k from 2): its
    record dir with fresh mtimes and its halt-withheld index entry with a fresh `at_ns`. Writes only
    under `clone`. Returns the in-window halted count after scaling (`scale` times the original)."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.halted_withheld_index import INDEX_DIR

    if scale < 1:  # no test yet: belongs in test_land_run_leg_budget_profile.py
        raise ValueError(f"--halted-scale must be >= 1, got {scale}")
    runs = in_window_halted_runs(clone)
    for run in runs:
        for k in range(2, scale + 1):
            twin = f"{run}-x{k}"
            shutil.copytree(clone / RECORD_DIR_ROOT / run, clone / RECORD_DIR_ROOT / twin, copy_function=shutil.copy, dirs_exist_ok=True)
            entry = clone / INDEX_DIR / f"{run}.json"
            if entry.is_file():
                body = _json_of(entry)
                body["at_ns"] = time.time_ns()
                (clone / INDEX_DIR / f"{twin}.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return len(runs) * scale


def seed_parse_verdicts(clone: Path) -> int:
    """Fill the clone's parse-verdict cache for each dirty `.py` the way the run's earlier producers
    (`measured_rev`, `review_prep`) do over the same bytes, before the timed land. Returns the `.py`
    count parsed; the parse itself is not part of any timed leg."""
    from coordinator_core.ops.dispatch_emit import landing_integrity

    status = run_git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=str(clone))
    if not status.ok:
        raise RuntimeError(f"git status failed in {clone}")
    dirty = [e[3:] for e in status.stdout.split("\0") if len(e) > 3 and e[3:].endswith(".py")]
    landing_integrity.unparseable_py(clone, dirty)
    return len(dirty)


def run_real_record(run_id: str, source_root: Path, attribute: bool, halted_scale: int = 1, seed_verdicts: bool = True) -> list:
    """Replay `run_id`'s land_run in a throwaway clone of `source_root`, stopping before the commit leg.
    `halted_scale` K multiplies the clone's in-window halted runs (`scale_halted_runs`).
    `seed_verdicts` False leaves the parse-verdict cache cold: the landing parses every changed `.py` itself."""
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.session import core as session_core

    declare_benchmark_origin()
    _bind_session()
    params = real_record_params(source_root, run_id)
    with ExitStack() as stack:
        clone = enter_record_fixture(stack, source_root, params, prefix="lrrr", label="land_run real-record")
        seed_real_record(source_root, clone, run_id)
        halted_in_window = scale_halted_runs(clone, halted_scale)
        seeded = seed_parse_verdicts(clone) if seed_verdicts else 0
        session_core.init(SELF, cwd=str(clone))

        def call(root: Path, _base: str) -> dict:
            land_run._launch_close_job = lambda *a, **k: True
            return land_run._handler(dict(params), repo_root=root / ".git")

        rows = profile_land(clone, params["run_base_sha"], attribute=attribute, call=call, stop_before_commit=True, legs=True)
    for row in rows:
        row.attribution["run_id"] = run_id
    rows[0].attribution["halted_in_window"] = halted_in_window
    rows[0].attribution["halted_scale"] = halted_scale
    rows[0].attribution["parse_verdicts_seeded_py"] = seeded
    return rows


def render(rows: list) -> str:
    out = [f"{'axis':42} {'proc_ms':>9} {'spawns':>6} {'parses':>6}  note"]
    for r in rows:
        bar = LEG_BAR_MS if r.axis != WHOLE_OP else BAR_MS
        if r.attribution.get("excepted"):
            bar = float("inf")
        calls = r.attribution.get("calls", [])
        note = r.attribution.get("outcome") or (
            "not_reached" if r.attribution.get("not_reached") else f"calls={calls if isinstance(calls, int) else len(calls)}" if "calls" in r.attribution else ""
        )
        out.append(f"{r.axis:42} {r.process_time_ms:9.1f} {r.spawns:6d} {r.spine_parses:6d}  {'OVER ' if r.process_time_ms > bar else 'ok '}{note}")
    for r in rows:
        for leg in r.attribution.get("legs", []):
            out.append(f"  leg {leg['leg']:42} -> {leg['to']:40} {leg['process_time_ms']:9.1f}ms {leg['spawns']:4d} spawns")
    for r in rows:
        for f in r.attribution.get("top_functions", []):
            out.append(f"  [{r.axis}] {f['cum_ms']:8.1f} cum {f['self_ms']:8.1f} self {f['calls']:6d}x {f['fn']}")
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--siblings", type=int, default=None, help=f"unlanded sibling runs with a spine and no manifest (default {DEFAULT_SIBLINGS}, 0 with --live-siblings)")
    ap.add_argument("--live-siblings", type=int, default=0, metavar="N", help="N sibling processes landing their own runs in the same tree as the measured land_run")
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--worker-root", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--worker-base", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--worker-sync", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--tracked", type=int, default=DEFAULT_TRACKED)
    ap.add_argument("--chain", type=int, default=DEFAULT_CHAIN)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--attribute", action="store_true", help="cProfile top-20 per span")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--real-record", default=None, metavar="RUN_ID", help="replay a recorded run read-only, stopping before the commit leg")
    ap.add_argument("--source-repo", default=str(_REPO), help="repo whose HEAD and run record --real-record clones")
    ap.add_argument("--halted-scale", type=int, default=1, metavar="K", help="with --real-record: multiply the clone's in-window halted runs K-fold")
    ap.add_argument("--cold-parse", action="store_true", help="with --real-record: skip seeding the parse-verdict cache (the landing parses every changed .py)")
    args = ap.parse_args(argv)
    if args.worker:
        return _worker(args.worker, Path(args.worker_root), args.worker_base, Path(args.worker_sync), args.attribute)
    siblings = args.siblings if args.siblings is not None else (0 if args.live_siblings else DEFAULT_SIBLINGS)
    if args.real_record:
        rows = run_real_record(args.real_record, Path(args.source_repo), args.attribute, args.halted_scale, not args.cold_parse)
    else:
        workdir = Path(tempfile.mkdtemp(prefix="lrlb-", dir=args.dir))
        try:
            if args.live_siblings:
                rows = run_live(workdir, args.live_siblings, siblings, args.tracked, args.chain, args.attribute)
            else:
                rows = run(workdir, siblings, args.tracked, args.chain, args.attribute)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    print(json.dumps([asdict(r) for r in rows], indent=1) if args.json else render(rows))
    if kill_gate(rows):
        print(f"KILL-GATE: {WHOLE_OP} over {BAR_MS:.0f}ms process time (DR-344)", file=sys.stderr)
        return 2
    return 0 if all(r.process_time_ms < LEG_BAR_MS for r in rows if r.axis != WHOLE_OP and not r.attribution.get("excepted")) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
