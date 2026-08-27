"""C1 of docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md --
where the dead commit op's 421.9ms actually went.

THE QUESTION. `ceremony.commit` was killed at p50 421.9ms process time (n=241,
`op_budget_suspension.py`). The commit work itself measures 14-21ms in-process
(`001b0a669`, bracketed). Nothing attributes the ~400ms difference, and the
whole plan turns on it: if it lives in the 11,015-line pipeline, C8 deletes
those lines; if it lives in DISPATCH -- envelope, interpreter start, registry
preload, pipe round trip -- then deleting them buys nothing measurable and the
honest v2 is a dispatch plan instead.

WHAT THIS MEASURES. The dead op cannot be dialed (it answers -32004/-32006), so
this measures the ENVELOPE directly instead of by subtraction: several LIVE ops
through the same installed door, at the same warm server, job-accounted. If
every live op through the door costs tens of ms, dispatch is not where 400ms
went. If they cost hundreds, it is.

BRACKETED, NEVER PER-CALL. Job accounting quantises to a ~15.625ms tick, so a
`snapshot()` pair around ONE dispatch returns a tick COUNT, not a cost, and a
median over such samples picks the low mode -- biased downward, and not fixed by
taking more samples. Two figures on this exact surface read 1x and 2x the tick,
were retracted at `001b0a669`, and the bracketed re-run showed one was nearly
double. So: one snapshot before N dispatches, one after, divide by N; repeat the
window; report the spread.

ATTACHED TO THE SERVER, NOT THE CLIENT. `coordinator-invoke.exe` is a JSON-RPC
framer over a pipe. The op runs in the warm SERVER, and anything it spawns is
the server's descendant. A job object around the client measures the framer and
reports it as the op.

ATTACH BEFORE WARMTH, NOT AFTER (CORRECTED). `_pool_dispatch`
(`coordinator_core/warm/server.py`) builds its `ProcessPoolExecutor` lazily on
the first dispatch, and the POOL WORKER -- not the server process -- executes
every op measured here. Job-object membership is fixed at process creation, so
a worker spawned before the accountant attaches is invisible to it forever.
This test used to fire its warmth `ping` BEFORE attaching the accountant,
which measured only the server's own pipe/framing CPU and published it as the
envelope: ~3.1ms/call. Corrected to attach first, the pool workers land inside
the job and the honest envelope reads ~5.6ms/call (median across 3 windows,
N=40) -- roughly 2x the pre-fix figure. The verdict this spike answers is
unchanged either way (envelope << 421.9ms), but the number itself was
understated by the ordering bug, not by noise.

NEGATIVE SPEC: writes no production code, registers no op, touches nothing under
`ops/`. A spike that leaves production code behind has become the build.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator, List, Optional

import pytest

from coordinator_core.benchmarks.process_time import IS_WINDOWS, LiveTreeAccountant
from coordinator_core.benchmarks.isolated_clone import (
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
)
from coordinator_core.warm import breadcrumb

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BOOT_WAIT_SECS = 90.0
_POLL_SECS = 0.2
_SUBPROCESS_TIMEOUT_S = 120

WINDOW_N = 40
"""Dispatches per job window. The tick is ~15.625ms, so N=40 divides the
quantisation error by 40 -- roughly +/-0.4ms at the reported per-call figure,
which is resolvable against a 50ms bar where a single call is not."""

WINDOWS = 3
"""Independent windows. The spread across them is the honest error bar; a single
window's figure is a point estimate with no way to see its own noise."""

_INSTALLED_DOOR_EXE = (
    Path(os.path.expanduser("~"))
    / ".coordinator-claude-settings"
    / "bin"
    / "coordinator-invoke.exe"
)
"""Resolved at import, before the suite root's conftest quarantines HOME into a
throwaway dir -- a `Path.home()` inside a fixture body resolves to the
quarantine and silently skips this gate on a box that has the door."""

ARMS = [
    ("ping", "{}", "envelope floor -- no repo, no params, no work"),
    ("queue.age_ping", "{}", "live op, light work"),
    ("fleet.archive_sweep_status", "{}", "live op, reads disk"),
]
"""Live ops only. Every op in the kill batch is drained from the registration
surfaces, so the dead commit op cannot be an arm here -- which is the point:
this bounds the envelope directly rather than subtracting it out of a figure
that no longer reproduces."""

DEAD_OP_P50_MS = 421.9
COMMIT_LEG_MS = (14.06, 16.80, 21.09)


def _require_windows() -> None:
    if not IS_WINDOWS:
        pytest.skip("job-object accounting is Windows-only")


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


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


def _write_isolated_stamp(isolated_root: Path) -> None:
    """A valid `_engine_stamp` in the ISOLATED destination only. Without it the
    door's root validation fails SILENTLY and the dial is served by the
    machine's PUBLISHED engine instead -- measuring a different tree under this
    tree's label."""
    (isolated_root / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:commit-v2-floor-spike-{uuid.uuid4().hex}\n", encoding="utf-8"
    )


def _env(engine_root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_WARM"] = "1"
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    env.pop("VIRTUAL_ENV", None)
    return env


@pytest.fixture(scope="module")
def warm_root() -> Iterator[tuple]:
    _require_windows()
    source_root = _source_root()
    if not (source_root / "coordinator_core").is_dir():
        pytest.skip(f"{source_root} carries no coordinator_core/ to hardlink")

    tmp_parent = mkdtemp_for_clone(source_root, prefix="commit-v2-floor-spike-")
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    try:
        _hardlink_coordinator_core(source_root, root)
        _write_isolated_stamp(root)
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.warm.server"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        deadline = time.time() + _BOOT_WAIT_SECS
        crumb = None
        while time.time() < deadline:
            crumb = breadcrumb.read_breadcrumb(engine_root=root)
            if crumb and crumb.get("pid"):
                break
            time.sleep(_POLL_SECS)
        if not (crumb and crumb.get("pid")):
            pytest.skip(f"isolated warm server did not boot within {_BOOT_WAIT_SECS}s")
        # Captured HERE and yielded, never re-read in the test body: the
        # breadcrumb is the server's own liveness record and re-reading it later
        # returned None on this box, which is a fact about the breadcrumb's
        # lifetime, not about the server -- the process is still up and serving.
        yield root, int(crumb["pid"])
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            try:
                import psutil

                p = psutil.Process(int(crumb["pid"]))
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="commit_v2_floor_spike", reaped=reaped)


def _dispatch(door: Path, op: str, params: str, root: Path, env: dict):
    return subprocess.run(
        [str(door), op, params],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        creationflags=_CREATE_NO_WINDOW,
    )


def _bracketed_window(
    accountant: LiveTreeAccountant,
    door: Path,
    op: str,
    params: str,
    root: Path,
    env: dict,
    n: int,
) -> dict:
    """N dispatches inside ONE job window. The whole point: divide the tick
    error by n rather than paying it per sample."""
    before = accountant.snapshot()
    rcs = []
    for _ in range(n):
        rcs.append(_dispatch(door, op, params, root, env).returncode)
    after = accountant.snapshot()
    return {
        "ms_per_call": round(
            (after["process_time_ms"] - before["process_time_ms"]) / n, 3
        ),
        "procs_per_call": round((after["procs"] - before["procs"]) / n, 3),
        "rc_ok": sum(1 for r in rcs if r == 0),
        "n": n,
    }


def test_c1_where_the_dead_commit_ops_400ms_went(warm_root) -> None:
    warm_root, server_pid = warm_root
    door = _INSTALLED_DOOR_EXE
    if not door.exists():
        pytest.skip(f"{door} not installed on this box")
    env = _env(warm_root)

    rows = []
    with LiveTreeAccountant(server_pid) as acct:
        # Warmth is a PRECONDITION, asserted not requested: a cold dispatch is
        # indistinguishable from a warm one by inspecting request env alone, and
        # every figure here would carry a "warm" label it had not earned.
        #
        # ATTACH-BEFORE-WARMTH (corrected ordering). `_pool_dispatch` builds its
        # ProcessPoolExecutor lazily on the FIRST dispatch (`_ensure_dispatch_pool`
        # in `coordinator_core/warm/server.py`), and the POOL WORKER -- not the
        # server process -- executes every op this test measures
        # (`_declare_execution_route`'s docstring). `LiveTreeAccountant` counts by
        # job-object membership, which is fixed at process creation: a process
        # spawned BEFORE attachment is never a job member no matter how long the
        # accountant later runs. This warmth probe is the FIRST dispatch through
        # this isolated server, so it is what spawns the pool -- the accountant
        # must already be attached when it fires, or the pool workers are born
        # outside the job and every arm below measures pipe/framing CPU wearing
        # the label "envelope". This is not hypothetical: measured on two
        # isolated servers, same op, same N=40, 3 windows each, ordering was the
        # only variable -- attach-before-probe read 5.08/4.30/7.42 ms/call,
        # attach-after-probe (the prior ordering of this test) read
        # 2.73/3.52/1.56 ms/call. The prior ordering understated the envelope by
        # roughly 2x.
        procs_before_probe = acct.snapshot()["procs"]
        probe = _dispatch(door, "ping", "{}", warm_root, env)
        assert probe.returncode == 0, (
            f"warmth precondition FAILED: ping through the door rc={probe.returncode} "
            f"stdout={probe.stdout[:300]!r} stderr={probe.stderr[:300]!r}"
        )
        # Do not trust the ordering above by inspection alone -- assert the pool
        # workers actually landed inside the job. `procs` is `TotalProcesses`,
        # cumulative since attachment (module docstring); the warmth probe is
        # the pool's first dispatch, so a first-dispatch pool build must grow
        # this count past the root alone. A regression back to attach-after
        # would spawn the pool before this accountant existed and this
        # assertion would catch it by finding no growth.
        procs_after_probe = acct.snapshot()["procs"]
        assert procs_after_probe > procs_before_probe, (
            "pool workers were not observed inside the job after the warmth "
            f"probe: procs before={procs_before_probe} after={procs_after_probe} "
            "-- the accountant may have attached AFTER the pool was already "
            "spawned, which silently excludes the workers doing every op's "
            "real work from every figure this test reports"
        )

        for op, params, why in ARMS:
            windows = [
                _bracketed_window(acct, door, op, params, warm_root, env, WINDOW_N)
                for _ in range(WINDOWS)
            ]
            rows.append({"op": op, "why": why, "windows": windows})

    print("\n" + "=" * 78)
    print("C1 -- ENVELOPE THROUGH THE INSTALLED DOOR (process time, job-object)")
    print(f"bracketed: {WINDOWS} windows x {WINDOW_N} dispatches, divided by N")
    print("=" * 78)
    for r in rows:
        ms = [w["ms_per_call"] for w in r["windows"]]
        procs = [w["procs_per_call"] for w in r["windows"]]
        ok = sum(w["rc_ok"] for w in r["windows"])
        tot = sum(w["n"] for w in r["windows"])
        print(
            f"{r['op']:<28} {' / '.join(f'{m:.2f}' for m in ms):>26} ms/call   "
            f"procs {max(procs):.2f}   rc0 {ok}/{tot}"
            f"{'':<28} [{r['why']}]"
        )

    envelope = statistics.median(
        [w["ms_per_call"] for r in rows for w in r["windows"] if r["op"] == "ping"]
    )
    print("-" * 78)
    print(f"envelope (ping) median, job-object CPU across the whole server tree "
          f"[JOB AXIS] ....... {envelope:.2f} ms")
    print(f"commit leg, in-process ....... {'/'.join(f'{m:.2f}' for m in COMMIT_LEG_MS)} ms")
    print(
        f"dead op, through dispatch, time.process_time() of the measuring "
        f"process ONLY -- excludes every git child and every conhost "
        f"[SINK AXIS] .... {DEAD_OP_P50_MS} ms  (n=241)"
    )
    # NOT a subtraction of the two figures above: the sink figure
    # (`ipc.py`'s recorded 421.9ms) is `time.process_time()` of ONE process,
    # excluding every spawned child, while the envelope figure above is
    # job-object CPU across the WHOLE server tree -- different axes, and
    # `dead_op - envelope - commit_leg` mixes them as if they were the same
    # unit. Each is reported on its own axis instead. The mismatch runs
    # CONSERVATIVE for the plan's own verdict: the sink figure already
    # excludes all child CPU, so its ~421.9ms is pure Python time inside one
    # pool worker with none of the envelope's cost even eligible to be
    # counted in it -- which makes the case for deleting the pipeline
    # STRONGER, not weaker, than a naive subtraction would suggest.
    print(
        "NOTE: the two figures above are different axes (job-object tree CPU "
        "vs one process's own process_time()) and are not subtracted here -- "
        "see comment above this print for why, and why the mismatch is "
        "conservative for the delete case."
    )
    print(
        "VERDICT: "
        + (
            "envelope is NOT the cost -- the gap is the op's own work, C8 justified"
            if envelope < 100
            else "envelope DOMINATES -- deleting the pipeline buys little, C8 drops"
        )
    )
    print("=" * 78)

    # `ping` is the arm the verdict rests on and it must be clean. The other
    # arms are context: a live op can legitimately return non-zero for its own
    # reasons (missing params, nothing to do), and that says nothing about the
    # ENVELOPE, which is paid before the handler ever runs. Their rc rate is
    # printed above rather than asserted, so a degraded arm is visible instead
    # of either failing the run or silently flattering it.
    ping_rows = [r for r in rows if r["op"] == "ping"]
    for r in ping_rows:
        for w in r["windows"]:
            assert w["rc_ok"] == w["n"], f"ping had failed dispatches: {w}"
    for r in rows:
        for w in r["windows"]:
            assert w["ms_per_call"] > 0.0, (
                f"{r['op']} measured 0.00ms/call -- a bracketed window cannot be "
                f"free; the instrument is not attached to the work. {w}"
            )
