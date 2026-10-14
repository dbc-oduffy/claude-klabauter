"""Standing gate: the workweek-complete doc-staleness step under the 500ms bar, warm-engine end-to-end.

The fixture (`doc_staleness_full_path`) is a real-scale history behind README.md and INSTALL.md at
the depths docs/research/2026-10-14-doc-staleness-rebuild-spike.md § Q5 measured. The CLI is
reached the way it ships: the installed native door, named for the CLI, relays argv to a warm
engine (`invoke.from_argv` with `params.entrypoint`), booted here from a hardlink clone of this
tree so the op under test is the working-tree op.

Basis (requirements ledger R1): per sample, reach_ms is the door client's own tree process time
(`batched_process_time_ms`; its procs_per_call must stay within _REACH_MAX_PROCS, else the door fell back to a cold CLI) and
op_process_ms is the engine's job-object delta (`LiveTreeAccountant`, git children included) across
the same calls. total = reach + op; nothing is subtracted, and the median over SAMPLES samples is
gated. Each sample brackets K_CALLS calls and divides, because job accounting lands on ~15.6ms
ticks. Process time and spawn count only, never wall clock.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.

The fixture follows this repo's shape: merges at its ratio, a split commit-graph, no Bloom filters.
Run it explicitly with `-m cadence`.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

import pytest

from coordinator_core.benchmarks.doc_staleness_full_path import (
    ENTRYPOINT,
    check_report,
    ensure_fixture,
)
from coordinator_core.benchmarks.isolated_clone import (
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
)
from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    LiveTreeAccountant,
    batched_process_time_ms,
)
from coordinator_core.warm import breadcrumb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_CALLS = 3
SAMPLES = 5
BUDGET_MS = 500.0

_REACH_MAX_PROCS = 2  # the door client plus the console host Windows allocates for it
_REACH_MAX_MS = 100.0  # a cold CLI fallback pays an interpreter start well above this
_BOOT_WAIT_SECS = 90.0
_CALL_TIMEOUT_S = 120

# Resolved at import: the suite root's conftest quarantines HOME for unmarked tests.
_SETTINGS_HOME = Path(
    os.environ.get("COORDINATOR_SETTINGS_HOME")
    or Path(os.path.expanduser("~")) / ".coordinator-claude-settings"
)
_DOOR_DIR = _SETTINGS_HOME / "bin"
_ENTRY_DOOR = _DOOR_DIR / f"{ENTRYPOINT}.exe"
_PING_DOOR = _DOOR_DIR / "coordinator-invoke.exe"


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _link_tree(src: Path, dst: Path, *, skip_dirs: tuple[str, ...] = ("__pycache__",)) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        out = dst / Path(root).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            os.link(Path(root) / name, out / name)


def _clone_engine(source_root: Path, root: Path) -> None:
    """The engine package plus the one CLI script and its `lib`, which the entrypoint resolver reads."""
    _link_tree(source_root / "coordinator_core", root / "coordinator_core")
    bin_src = source_root / "coordinator" / "bin"
    bin_dst = root / "coordinator" / "bin"
    bin_dst.mkdir(parents=True)
    os.link(bin_src / f"{ENTRYPOINT}.py", bin_dst / f"{ENTRYPOINT}.py")
    _link_tree(bin_src / "lib", bin_dst / "lib")
    (root / "coordinator_core" / "_engine_stamp").write_text(
        f"sha:doc-staleness-process-budget-{time.time_ns()}\n", encoding="utf-8"
    )


def _env(engine_root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_WARM"] = "1"
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    env.pop("VIRTUAL_ENV", None)
    return env


@pytest.fixture(scope="module")
def warm_root() -> Iterator[tuple[Path, int, dict]]:
    """An isolated warm engine booted with no dispatch fired, so the accountant attaches before
    the pool workers that execute the op exist (job membership is fixed at process creation)."""
    if not IS_WINDOWS:
        pytest.skip("job-object accounting is Windows-only")
    if not (_ENTRY_DOOR.exists() and _PING_DOOR.exists()):
        pytest.skip(f"{_ENTRY_DOOR} / {_PING_DOOR} not installed on this box")
    source_root = _source_root()
    tmp_parent = mkdtemp_for_clone(source_root, prefix="doc-staleness-budget-")
    root = tmp_parent / "clone"
    proc: Optional[subprocess.Popen] = None
    try:
        _clone_engine(source_root, root)
        proc = subprocess.Popen(
            [sys.executable, "-m", "coordinator_core.warm.server"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.time() + _BOOT_WAIT_SECS
        crumb = None
        while time.time() < deadline:
            crumb = breadcrumb.read_breadcrumb(engine_root=root)
            if crumb and crumb.get("pid"):
                break
            time.sleep(0.2)
        if not (crumb and crumb.get("pid")):
            pytest.skip(f"isolated warm engine did not boot within {_BOOT_WAIT_SECS}s")
        yield root, int(crumb["pid"]), _env(root)
    finally:
        crumb = breadcrumb.read_breadcrumb(engine_root=root)
        if crumb and crumb.get("pid"):
            try:
                import psutil

                p = psutil.Process(int(crumb["pid"]))
                # Workers are signalled while their parent is alive: an orphan has left the
                # test's process tree and the suite's foreign-kill tripwire refuses it.
                workers = p.children(recursive=True)
                for w in workers:
                    w.terminate()
                p.terminate()
                psutil.wait_procs([p, *workers], timeout=5)
            except Exception:
                pass
        elif proc is not None and proc.poll() is None:
            proc.terminate()
        reaped = reap_processes_under(tmp_parent)
        shutil.rmtree(breadcrumb.svc_dir(engine_root=root), ignore_errors=True)
        rmtree_or_raise(tmp_parent, label="doc_staleness_process_budget", reaped=reaped)


def _fixture(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("doc_staleness_budget")
    built = subprocess.run(
        [sys.executable, "-m", "coordinator_core.benchmarks.doc_staleness_full_path", str(workdir), "--build"],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert built.returncode == 0, f"fixture build failed: {built.stderr}"
    return ensure_fixture(workdir)


@pytest.fixture(scope="module")
def staleness_measurement(warm_root, tmp_path_factory):
    _root, server_pid, env = warm_root
    fixture = _fixture(tmp_path_factory)
    cmd = [str(_ENTRY_DOOR), "--root", str(fixture)]
    samples = []
    with LiveTreeAccountant(server_pid) as acct:
        before_probe = acct.snapshot()["procs"]
        probe = subprocess.run(
            [str(_PING_DOOR), "ping", "{}"], capture_output=True, text=True, env=env,
            timeout=_CALL_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert probe.returncode == 0, (
            f"warmth probe failed rc={probe.returncode}: {probe.stderr[:300]!r}"
        )
        assert acct.snapshot()["procs"] > before_probe, (
            "no engine process observed inside the job after the probe: the accountant "
            "attached after the pool spawned, or the probe was not served warm"
        )
        served = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=_CALL_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert served.returncode == 0, f"door call rc={served.returncode}: {served.stderr[:300]!r}"
        problem = check_report(json.loads(served.stdout))
        assert problem is None, f"gate would time a wrong result: {problem}"
        for _ in range(SAMPLES):
            before = acct.snapshot()
            reach = batched_process_time_ms(cmd, k=K_CALLS, env=env)
            after = acct.snapshot()
            assert reach["rc"] == 0, f"door call rc={reach['rc']}"
            samples.append({
                "reach_ms": reach["process_time_ms"],
                "reach_procs": reach["procs_per_call"],
                "op_ms": (after["process_time_ms"] - before["process_time_ms"]) / K_CALLS,
                "op_procs": (after["procs"] - before["procs"]) / K_CALLS,
            })
    for s in samples:
        s["total_ms"] = round(s["reach_ms"] + s["op_ms"], 3)
        s["spawns"] = round(s["reach_procs"] + s["op_procs"], 3)
    med = lambda key: round(statistics.median(s[key] for s in samples), 3)  # noqa: E731
    result = {k: med(k) for k in ("reach_ms", "op_ms", "total_ms", "spawns", "reach_procs", "op_procs")}
    result["detail"] = (
        f"doc-staleness step, warm-engine end-to-end: median total {result['total_ms']}ms = "
        f"reach_ms {result['reach_ms']} + op_process_ms {result['op_ms']}; spawns {result['spawns']} "
        f"(reach {result['reach_procs']} + op {result['op_procs']}); "
        f"samples={SAMPLES} k={K_CALLS}; totals {[s['total_ms'] for s in samples]}"
    )
    result["samples"] = samples
    print(f"DOC-STALENESS-BUDGET {result['detail']}")
    return result


def test_door_reach_is_warm(staleness_measurement):
    m = staleness_measurement
    assert all(s["reach_procs"] <= _REACH_MAX_PROCS and s["reach_ms"] < _REACH_MAX_MS for s in m["samples"]), (
        f"the door did more than relay (cold CLI fallback): {m['detail']}"
    )
    assert m["op_procs"] >= 1, f"no git child charged to the engine: {m['detail']}"


def test_doc_staleness_is_under_the_500ms_bar(staleness_measurement):
    m = staleness_measurement
    assert m["op_ms"] > 0 and m["op_procs"] > 0, f"op cost not observed in the engine: {m['detail']}"
    assert m["total_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
