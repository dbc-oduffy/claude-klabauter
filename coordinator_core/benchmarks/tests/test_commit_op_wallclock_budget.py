"""
coordinator_core.benchmarks.tests.test_commit_op_wallclock_budget

C2 of docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md — "Give
the op a real wallclock and spawn baseline through the warm transport."

WHAT THIS FILE WAS, AND WHAT C6 CHANGES. This chunk was originally
NOT-contingent instrument-only (C2's own body: "it cannot invalidate what
follows") — C1 wired `ceremony.commit` to `run_commit_pipeline` UNCHANGED,
so every sample here paid the pipeline's two-git-invocation agree branch and
was expected to be BAD by construction. C3 (in-process object write) and C5
(safe-commit routing) have since landed. C6
(docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md) is the chunk
that reads these numbers against the plan's own AC4/AC7 targets and closes
them — so the two numeric gates below (AC4's 150ms wallclock median, AC7's
"AC4's target holds under concurrent load") are now REAL assertions, not
recorded-only instrument sanity.

MEASURED 2026-08-26, POST-C3/C5, this repo's own isolated warm server (this
file's own `warm_engine_root` fixture): wallclock median 551.6ms / p95
849.4ms (target 150ms), process_time 695.3ms at 35.75 job-object
processes/call, and an 8-way concurrent p50 of 3959.1ms / p95 3990.7ms —
each roughly 3.7-26x over AC4's target, not a rounding-distance miss. AC5's
dial leg alone (a zero-git `ping` through the identical door) measures 1
process / ~82ms, so the transport itself is not the residual cost: the
excess sits inside the commit op's own handler path (the four commit-leg
gates, trailer assembly, and/or hooks still walking `subprocess.run` rather
than the in-process object-write machinery C3 shipped for the commit itself)
— a design/mechanism gap in files this chunk's `writes:` scope excludes
(`commit_op.py`, `git_native.py`, `commit_pipeline.py`), not a measurement
artifact of this instrument. Recorded here, gated below, and reported back
to the plan rather than silently loosened: per the plan's own AC4 section,
"the answer is to make the op cheaper... not to widen AC4."

THREE COLUMNS, NEVER COLLAPSED (plan task body, verbatim instruction).
wallclock (median/p50/p95), process time, and job-object spawn count are
three separate measurements answering three separate questions —
`coordinator_core.benchmarks.process_time` module docstring: process time
and spawn count read peer-load-free cost; wallclock is the only axis that
sees ENGINE QUEUEING, which is invisible to both of the others (a resident
server serialising concurrent commits reaches its own door in 3.9ms but can
then wait behind peers). No number in this file substitutes for another.

INSTRUMENT: `python -m coordinator_core.invoke ceremony.commit <params>
--repo <path>` — the real warm-attempt-then-cold-fallback CLI door
(`coordinator_core.invoke.__main__`, the same entrypoint
`coordinator_core.benchmarks.timer.time_invocation` already times end to
end), never a direct in-process call to `run_commit_pipeline` or the op
handler — AC4 says "warm-served", and only the CLI door actually attempts a
warm dial before falling back cold.

WARM SERVER: this live tree carries no `coordinator_core/_engine_stamp`
(DR-315 §2 — an unstamped dev checkout is not a warm-server HOST, so every
call FROM it goes cold unconditionally, per `test_op_cli_warm_hop_process_
time.py`'s own C4 finding). `warm_engine_root` below duplicates
`test_warm_door_process_time_gate.py::warm_engine_root`'s own isolation
recipe (hardlink `coordinator_core/` into a fresh temp dir, boot `python -m
coordinator_core.warm.server` against it, poll for a PID-alive breadcrumb)
rather than importing that fixture — a pytest fixture function cannot be
called directly outside pytest's own fixture protocol (this pytest version
raises "Fixture ... called directly"), the same constraint that module's
own `_short_runtime_base` docstring records for its own DELIBERATE
DUPLICATE. ONE DELIBERATE DIVERGENCE from that recipe: this file hardlinks
`coordinator_core/` from THIS LIVE DEV TREE, never a published sibling
clone, and stamps the isolated destination itself (`is_engine_root`'s own
contract — `engine_root.py`: "only its BYTES matter... readable and
non-empty", no content validation) rather than requiring the SOURCE to
already carry one. `ceremony.commit` (this plan's own C1) exists only in
this dev tree's uncommitted/unpublished work — the published sibling mirror
`test_warm_door_process_time_gate.py` hardlinks FROM does not carry it yet
(confirmed empirically: dispatching against a hardlink of that mirror
returns `-32601 Method not found: 'ceremony.commit'`), so measuring THIS
op through a real warm server requires serving THIS tree's own bytes.
Skips (never fails) when this file's own `coordinator_core/` package is
absent, which cannot happen in a checked-out repo but is named for parity
with the sibling gate's skip discipline.

DRIVER SHAPE. `batched_process_time_ms` re-runs the SAME argv `k` times, so
per-invocation commit content must differ -- the driver mutates
`tracked.txt` to fresh content IN-PROCESS, then spawns `python -m
coordinator_core.invoke ceremony.commit ... --params-file <path>` as a
CHILD and exits with its own return code, forwarding stdout so this file's
callers can still read the JSON-RPC envelope. `--params-file` (not the
positional `params_json` argv form) is deliberate, not incidental: this
driver's params carry a JSON object with braces/spaces
(`invoke/__main__.py`'s own docstring names exactly this payload shape as
`--params-file`'s reason to exist -- quoting-immune, ARG_MAX-safe). The
driver's own interpreter start is therefore counted alongside the invoke
door's in every figure this file reports; unlike `test_commit_path_process_
budget.py`'s driver-residue calibration, this file does not subtract it out
-- C2 is establishing the instrument, not pinning an AC6-style ceiling
tight enough for a wrapper's own interpreter start to matter.

AC5's DIAL LEG: measured as `ping` (a "none"-scoped, zero-git, near-instant
op) against the SAME isolated warm server — the cost of reaching the engine
at all, reported separately from AC4's whole-commit total, per this file's
task body ("Reach to the engine alone... reported separately from AC4's
total").

AC7's CONCURRENT ARM: >=8 simultaneous commits from DISTINCT worktrees (git
worktree add off one shared repo, each pointed at its own fresh branch), all
dialing the SAME isolated warm server — the shape that exposes ENGINE
QUEUEING (a resident server serialising concurrent commits), which the
task body distinguishes from `index.lock` contention (a SEPARATE,
shared-worktree experiment this file does not run: distinct worktrees do
not share an index.lock).

Spec backlink: docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md,
C2. AC4/AC5/AC7 are read against this instrument by a later chunk (C6); this
file discharges C2's own task body only.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    batched_process_time_ms,
)
from coordinator_core.warm import breadcrumb
from coordinator_core.session.core import stable_pid_alive

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_WALLCLOCK_SAMPLES = 15
"""Plan task body: "wallclock median, p50, p95 ... k>=15"."""

K_PROCESS_TIME_INVOCATIONS = 8
"""Amortisation factor for the process-time/spawn-count leg — matches the
plan's own C3/C4 methodology (`k>=8`) elsewhere in this package."""

N_CONCURRENT = 8
"""AC7: "at least 8 simultaneous commits from distinct worktrees"."""

AC4_TARGET_MS = 150.0
"""Plan's own AC4 row: "Warm-served, the op commits in <=150ms wallclock
measured end-to-end from the caller." AC7 reuses this same figure -- its
own row is "AC4's wallclock holds ... under concurrent load", not a
distinct number."""

_ENGINE_ROOT_OVERRIDE_ENV = "COORDINATOR_WARM_GATE_ENGINE_ROOT"
_BOOT_WAIT_DEADLINE_SECS = 20.0
_BOOT_POLL_INTERVAL_SECS = 0.25
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBPROCESS_TIMEOUT_S = 60


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip(
            "process-time job-object accounting and this file's isolated-server "
            "boot recipe are Windows-only in this file (mirrors test_warm_door_"
            "process_time_gate.py's own Windows fixture); no Darwin/Linux leg "
            "is authored here"
        )


def _fwd(p) -> str:
    return str(p).replace("\\", "/")


def _git(repo, *args, check=True, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
        env=env,
        creationflags=_CREATE_NO_WINDOW,
    )


def _env(**overrides) -> dict:
    base = dict(os.environ)
    base.setdefault(
        "COORDINATOR_ENGINE_ROOT", str(Path(__file__).resolve().parents[2].parent)
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Isolated warm server — deliberate duplicate of test_warm_door_process_time_
# gate.py::warm_engine_root's own recipe (see module docstring: a pytest
# fixture cannot be called directly outside pytest's own protocol).
# ---------------------------------------------------------------------------


def _source_root() -> Path:
    """The tree to hardlink `coordinator_core/` FROM. An explicit override
    always wins (a different box's clone layout); otherwise THIS LIVE DEV
    TREE (module docstring's ONE DELIBERATE DIVERGENCE from the sibling
    gate's own recipe) — `ceremony.commit` exists only here, not on any
    published mirror this box may also carry."""
    override = os.environ.get(_ENGINE_ROOT_OVERRIDE_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3]


def _write_isolated_stamp(isolated_root: Path) -> None:
    """Manufactures a valid `_engine_stamp` in the ISOLATED destination
    only, never the source — `is_engine_root`'s own contract (module
    docstring) validates only readability/non-emptiness, not content, so
    this is a legitimate stamp, not a spoof of one."""
    stamp_path = isolated_root / "coordinator_core" / "_engine_stamp"
    stamp_path.write_text(f"sha:commit-op-wallclock-{uuid.uuid4().hex}\n", encoding="utf-8")


def _hardlink_coordinator_core(source_root: Path, isolated_root: Path) -> int:
    n = 0
    src_pkg = source_root / "coordinator_core"
    for root, dirs, files in os.walk(src_pkg):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = Path(root).relative_to(src_pkg)
        dst_dir = isolated_root / "coordinator_core" / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, dst_dir / name)
            n += 1
    return n


def _wait_for_live_breadcrumb(isolated_root: Path, deadline_secs: float) -> Optional[dict]:
    deadline = time.time() + deadline_secs
    while time.time() < deadline:
        crumb = breadcrumb.read_breadcrumb(engine_root=isolated_root)
        if crumb:
            pid = crumb.get("pid")
            epoch = crumb.get("stable_pid_start_epoch") or ""
            if pid is not None and stable_pid_alive(pid, stored_start_epoch=str(epoch)):
                return crumb
        time.sleep(_BOOT_POLL_INTERVAL_SECS)
    return None


def _terminate(pid: int) -> None:
    import psutil

    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        pass


@pytest.fixture(scope="module")
def warm_engine_root() -> Iterator[Path]:
    _require_windows()

    source_root = _source_root()
    if not (source_root / "coordinator_core").is_dir():
        pytest.skip(
            f"{source_root!r} carries no coordinator_core/ package to hardlink -- "
            f"point {_ENGINE_ROOT_OVERRIDE_ENV} at a real checkout to run this file"
        )

    tmp_parent = Path(tempfile.mkdtemp(prefix="commit-op-wallclock-", dir=str(source_root.parent)))
    isolated_root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    try:
        _hardlink_coordinator_core(source_root, isolated_root)
        _write_isolated_stamp(isolated_root)
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.warm.server"],
            cwd=str(isolated_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        crumb = _wait_for_live_breadcrumb(isolated_root, _BOOT_WAIT_DEADLINE_SECS)
        if crumb is None:
            if proc.poll() is None:
                proc.terminate()
            pytest.skip(
                f"isolated warm server (source={source_root}) did not reach a "
                f"PID-alive breadcrumb within {_BOOT_WAIT_DEADLINE_SECS}s"
            )
        yield isolated_root
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=isolated_root)
        if crumb and crumb.get("pid") is not None:
            _terminate(int(crumb["pid"]))
        elif proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        shutil.rmtree(breadcrumb.svc_dir(engine_root=isolated_root), ignore_errors=True)
        shutil.rmtree(tmp_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fixture repo(s) — one tracked file per worktree, a local bare origin (no
# network round trip), no hooks (DR-356: pre/post-commit hook cost is OUT of
# an op's own budget; this file measures the op, not the hook chain).
# ---------------------------------------------------------------------------


def _build_fixture_repo(tmp_path: Path, branch: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "c2-wallclock@example.com")
    _git(repo, "config", "user.name", "c2-wallclock")
    (repo / "tracked.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed commit")
    return repo


def _write_driver(driver_path: Path, repo: Path, counter_path: Path, isolated_root: Path) -> None:
    """Writes a driver that, IN-PROCESS, mutates `tracked.txt` to fresh
    content (so k identical-argv re-runs each produce a real, distinct
    commit — same idempotent-fixture trap `test_commit_path_process_budget
    .py::_write_driver` names), writes the params object to a sidecar file,
    then spawns the real invoke door via `--params-file` (module docstring:
    quoting-immune for a JSON payload carrying braces/spaces) and forwards
    its stdout/stderr/returncode verbatim."""
    script = f'''\
import json
import subprocess
import sys
from pathlib import Path

repo = r"{repo}"
counter_path = Path(r"{counter_path}")
n = int(counter_path.read_text()) if counter_path.exists() else 0
n += 1
counter_path.write_text(str(n))

Path(repo, "tracked.txt").write_text(f"driver rev {{n}}\\n", encoding="utf-8")

params = {{
    "subject": f"c2 wallclock baseline commit {{n}}",
    "stage_paths": ["tracked.txt"],
    "caller_paths": ["tracked.txt"],
    "push_mode": "none",
}}
params_path = counter_path.with_suffix(".params.json")
params_path.write_text(json.dumps(params), encoding="utf-8")

argv = [
    sys.executable, "-m", "coordinator_core.invoke", "ceremony.commit",
    "--params-file", str(params_path), "--repo", repo, "--allow-unstamped-dispatch",
]
completed = subprocess.run(
    argv, capture_output=True, text=True,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
sys.exit(completed.returncode)
'''
    driver_path.write_text(script, encoding="utf-8")


def _parse_invoke_stdout(stdout: str) -> dict:
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict), f"invoke stdout was not a JSON object: {stdout!r}"
    assert "error" not in parsed, f"ceremony.commit returned an error envelope: {stdout!r}"
    return parsed


def _percentile(ordered: List[float], pct: float) -> float:
    """Nearest-rank, round-half-up — matches `process_time.py ::
    batched_process_time_quantiles._percentile`'s own tie-break rationale
    (Python's `round()` is ties-to-even, which silently mis-picks at small n)."""
    if len(ordered) == 1:
        return ordered[0]
    idx = math.floor(pct * (len(ordered) - 1) + 0.5)
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def _wallclock_samples(cmd: List[str], k: int, cwd: str, env: dict) -> dict:
    """Spawn-to-exit wall clock, one sample per real invocation (never a
    batched/amortised figure — AC4/AC7 need PER-CALL quantiles, not a mean).
    Verifies rc==0 and a real (non-error) JSON-RPC envelope for every
    sample, same AC9-style discipline `timer.py::time_invocation` applies."""
    samples_ms: List[float] = []
    for _ in range(k):
        t0 = time.perf_counter()
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert completed.returncode == 0, (
            f"driver invocation failed rc={completed.returncode}: "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
        _parse_invoke_stdout(completed.stdout)
        samples_ms.append(elapsed_ms)

    ordered = sorted(samples_ms)
    return {
        "n": k,
        "samples_ms": samples_ms,
        "median_ms": round(statistics.median(ordered), 3),
        "p50_ms": round(_percentile(ordered, 0.50), 3),
        "p95_ms": round(_percentile(ordered, 0.95), 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def test_commit_op_wallclock_and_spawn_baseline_are_recorded(
    tmp_path_factory, warm_engine_root
) -> None:
    """AC4's instrument, now GATED (C6 — module docstring's "MEASURED
    2026-08-26" note). Three separate columns from three separate
    measurement passes over the same driver/argv: wallclock quantiles (this
    pass, spawn-to-exit, k=15), process time, and job-object spawn count (a
    second pass, `batched_process_time_ms`, k=8) — never collapsed into one
    figure; only the wallclock column carries AC4's numeric gate.
    """
    tmp_path = tmp_path_factory.mktemp("commit_wallclock")
    repo = _build_fixture_repo(tmp_path, "work/c2-wallclock-baseline")
    env = _env()

    wall_driver = tmp_path / "driver_wall.py"
    wall_counter = tmp_path / "wall_counter.txt"
    _write_driver(wall_driver, repo, wall_counter, warm_engine_root)
    wall = _wallclock_samples(
        [sys.executable, str(wall_driver)],
        k=K_WALLCLOCK_SAMPLES,
        cwd=str(warm_engine_root),
        env=env,
    )

    proc_driver = tmp_path / "driver_proc.py"
    proc_counter = tmp_path / "proc_counter.txt"
    _write_driver(proc_driver, repo, proc_counter, warm_engine_root)
    proc = batched_process_time_ms(
        [sys.executable, str(proc_driver)],
        k=K_PROCESS_TIME_INVOCATIONS,
        cwd=str(warm_engine_root),
        env=env,
    )

    detail = (
        f"AC4 baseline (C1's unchanged pipeline, expected BAD -- module docstring): "
        f"wallclock median={wall['median_ms']}ms p50={wall['p50_ms']}ms "
        f"p95={wall['p95_ms']}ms min={wall['min_ms']}ms max={wall['max_ms']}ms "
        f"(n={wall['n']}). process_time={proc['process_time_ms']}ms "
        f"procs_per_call={proc['procs_per_call']} (k={proc['k']})."
    )
    print(detail)
    assert proc["rc"] == 0, f"process-time leg's driver must exit 0: {proc!r}. {detail}"
    assert wall["n"] == K_WALLCLOCK_SAMPLES
    assert wall["median_ms"] > 0.0, f"a zero-ms wallclock sample means the instrument is not measuring anything. {detail}"
    assert proc["procs_per_call"] >= 1.0, f"the op's own interpreter must count as at least one process. {detail}"
    # AC4 (C6): the real numeric gate, not a recorded-only baseline. See
    # module docstring's "MEASURED 2026-08-26" note for the current delta
    # and why it is a mechanism gap outside this chunk's writes: scope
    # rather than a loosening candidate.
    assert wall["median_ms"] <= AC4_TARGET_MS, (
        f"AC4 FAILS: wallclock median {wall['median_ms']}ms exceeds the "
        f"{AC4_TARGET_MS}ms target by {round(wall['median_ms'] - AC4_TARGET_MS, 3)}ms. {detail}"
    )


def test_commit_op_dial_leg_process_time_is_recorded(warm_engine_root) -> None:
    """AC5's instrument: the cost of REACHING the engine alone, reported
    separately from AC4's whole-commit total (module docstring). `ping` is
    a "none"-scoped op with zero git work -- the cheapest real round trip
    through the same warm-attempt-then-cold-fallback door AC4 measures,
    isolating dial cost from commit cost.
    """
    result = batched_process_time_ms(
        [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
        k=K_PROCESS_TIME_INVOCATIONS,
        cwd=str(warm_engine_root),
    )
    AC5_TARGET_MS = 60.0
    """DR-347 Ruling 1's amended figure (plan Problem section: "AC5 uses
    ~60ms"). Reported here, NOT gated: this AC is PROVISIONAL per this
    chunk's own brief, pending the sibling plan named in the parent plan's
    § Blocked by (docs/plans/2026-08-26-the-op-clis-dial-warm-from-the-
    process.md, chunks C1/C5/C6/C7/C9 landed per that plan's own tracker)."""
    delta = round(result["process_time_ms"] - AC5_TARGET_MS, 3)
    print(
        f"AC5 dial leg (PROVISIONAL, ungated -- see docstring): "
        f"process_time={result['process_time_ms']}ms procs_per_call="
        f"{result['procs_per_call']} vs ~{AC5_TARGET_MS}ms target (delta={delta}ms)"
    )
    assert result["rc"] == 0, f"AC5 dial-leg ping must exit 0: {result!r}"
    assert result["process_time_ms"] >= 0.0, (
        f"AC5 dial-leg baseline (reported separately from AC4's total, "
        f"PROVISIONAL -- see docstring): "
        f"process_time={result['process_time_ms']}ms procs_per_call="
        f"{result['procs_per_call']} (k={result['k']}) vs ~{AC5_TARGET_MS}ms "
        f"target (delta={delta}ms)"
    )


def test_commit_op_concurrent_load_wallclock_is_recorded(
    tmp_path_factory, warm_engine_root
) -> None:
    """AC7's instrument, now GATED on p50 (C6 -- module docstring's "MEASURED
    2026-08-26" note): >=8 simultaneous commits from DISTINCT worktrees
    (never a shared one -- `index.lock` contention is a separate experiment
    this file does not run, module docstring), all dialing the SAME
    isolated warm server, so engine queueing (not `index.lock`) is the term
    under measurement. Reports wallclock p50 AND p95 -- p95 is exposition
    only (queueing tail latency a p50/mean would hide); AC7's own text
    ("AC4's wallclock holds ... under concurrent load") gates p50 against
    AC4_TARGET_MS, the same figure AC4 itself gates.
    """
    tmp_path = tmp_path_factory.mktemp("commit_wallclock_concurrent")
    base_repo = _build_fixture_repo(tmp_path, "work/c2-wallclock-concurrent-base")
    env = _env()

    worktrees: List[Path] = []
    for i in range(N_CONCURRENT):
        branch = f"work/c2-wallclock-concurrent-{i}"
        wt_path = tmp_path / f"worktree-{i}"
        _git(base_repo, "worktree", "add", "-b", branch, str(wt_path), "HEAD", env=env)
        worktrees.append(wt_path)

    drivers = []
    for i, wt in enumerate(worktrees):
        driver = tmp_path / f"driver_concurrent_{i}.py"
        counter = tmp_path / f"concurrent_counter_{i}.txt"
        _write_driver(driver, wt, counter, warm_engine_root)
        drivers.append(driver)

    procs = []
    starts = []
    for driver in drivers:
        starts.append(time.perf_counter())
        procs.append(
            subprocess.Popen(
                [sys.executable, str(driver)],
                cwd=str(warm_engine_root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=_CREATE_NO_WINDOW,
            )
        )

    samples_ms: List[float] = []
    for start, proc in zip(starts, procs):
        stdout, stderr = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_S)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        assert proc.returncode == 0, (
            f"concurrent driver failed rc={proc.returncode}: stdout={stdout!r} stderr={stderr!r}"
        )
        _parse_invoke_stdout(stdout)
        samples_ms.append(elapsed_ms)

    ordered = sorted(samples_ms)
    p50 = round(_percentile(ordered, 0.50), 3)
    p95 = round(_percentile(ordered, 0.95), 3)
    detail = (
        f"AC7 concurrent-load baseline (n={len(ordered)} distinct worktrees, "
        f"expected BAD -- module docstring): wallclock p50={p50}ms p95={p95}ms "
        f"min={round(ordered[0], 3)}ms max={round(ordered[-1], 3)}ms samples={ordered}"
    )
    print(detail)
    assert len(ordered) == N_CONCURRENT, detail
    assert p95 >= p50, detail
    assert p50 > 0.0, f"a zero-ms concurrent sample means the instrument is not measuring anything. {detail}"
    # AC7 (C6): "AC4's wallclock holds under concurrent load" -- the same
    # target as AC4, read against p50 (p95 stays reported-only above: its
    # job is exposing queueing tail latency, not carrying a second gate).
    assert p50 <= AC4_TARGET_MS, (
        f"AC7 FAILS: concurrent-load wallclock p50 {p50}ms exceeds AC4's "
        f"{AC4_TARGET_MS}ms target by {round(p50 - AC4_TARGET_MS, 3)}ms under "
        f"{N_CONCURRENT}-way distinct-worktree load. {detail}"
    )
