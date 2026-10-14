"""Standing gate: what the warm engine charges an op beyond the op's own cost, in process time.

Per probe, overhead_ms is the engine job's process time per call (git children included) minus the
same op's job-accounted in-process cost per call, and unexplained_procs is the difference of the
two job process counts (the console host Windows allocates per git child cancels in it). Both bars
live in `engine_op_overhead` (OVERHEAD_BUDGET_MS, UNEXPLAINED_PROCS_BUDGET) and are never widened
here. Probes: `ping` (the engine path with zero git) and `doc-staleness` (invoke.from_argv plus
three run_git spawns, the op the requirement named). Each sample brackets K_CALLS calls and
divides, because job accounting lands on ~15.6ms ticks; the MEAN over SAMPLES is gated (the engine's
periodic skew refresh lands in one window in several, so a median hides it by construction). Process
time and job process counts only, never wall clock.

The dispatch pool is filled before the first counted sample by DISPATCH_PROCESS_POOL_SIZE
concurrent calls (sequential calls can leave it short); the warm-up processes are printed, never
asserted, so a lazy pool fill is boot cost and not a per-op figure. A worker start after the pool
is at size is a real per-op cost and counts against the unexplained-process bar.

Before the pool is counted it is also settled: the gate waits for two consecutive 1s windows under
250 ms/s of engine job process time, so the pool's boot-time preload CPU is boot cost and not billed
to the first probe.

Measured at bdaa51fb34 after the axis-2 skew cut (stat stamp, no git on a timer), mean over 5x3
calls: ping overhead 21-39 ms, doc-staleness 14-33 ms, unexplained_procs 0.000 on both. Run
explicitly with `-m cadence`.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from coordinator_core.benchmarks.engine_op_overhead import (
    OverheadSample,
    WarmEngine,
    in_process_sample_ms,
    isolated_warm_engine,
    over_budget,
)
from coordinator_core.benchmarks.process_time import IS_WINDOWS, LiveTreeAccountant
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_CALLS = 3
SAMPLES = 5
ENTRYPOINT = "workweek-complete-doc-staleness"
_FIXTURE_COMMITS = 30
_CALL_TIMEOUT_S = 120
_POOL_FILL_ROUNDS = 3
#: Question: "has the pool's boot-time preload CPU drained, so the counted window sees steady state?"
_SETTLE_WINDOW_S = 1.0
_SETTLE_MS_PER_S = 250.0
_SETTLE_TIMEOUT_S = 90.0

# Resolved at import: the suite root's conftest quarantines HOME for unmarked tests.
_SETTINGS_HOME = Path(
    os.environ.get("COORDINATOR_SETTINGS_HOME")
    or Path(os.path.expanduser("~")) / ".coordinator-claude-settings"
)
_DOOR_DIR = _SETTINGS_HOME / "bin"
_ENTRY_DOOR = _DOOR_DIR / f"{ENTRYPOINT}.exe"
_PING_DOOR = _DOOR_DIR / "coordinator-invoke.exe"

_PING_DRIVER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from coordinator_core.ops.ping import _ping
    n = int(sys.argv[sys.argv.index("--calls") + 1])
    for _ in range(n):
        _ping({})
    """
)

_DOC_STALENESS_DRIVER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from coordinator_core.ops.doc_registry import resolve_doc_registry_config
    from coordinator_core.ops.doc_staleness import build_doc_staleness_report
    root = sys.argv[1]
    n = int(sys.argv[sys.argv.index("--calls") + 1])
    for _ in range(n):
        registry = resolve_doc_registry_config(root)
        build_doc_staleness_report(
            root, registry.human_facing_docs or [],
            registry.doc_staleness_commits, registry.doc_staleness_days,
        )
    """
)


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _driver(template: str) -> str:
    return template.replace("{root!r}", repr(str(_source_root())))


def _git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(repo), *args],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, f"git {args!r} failed: {done.stderr[:300]}"


@pytest.fixture(scope="module")
def staleness_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("engine_overhead_repo")
    _git(repo, "init", "-q")
    for doc in ("README.md", "INSTALL.md"):
        (repo / doc).write_text(f"# {doc}\n" + "".join(f"Line {n}.\n" for n in range(20)), encoding="utf-8")
    _git(repo, "add", "README.md", "INSTALL.md")
    _git(repo, "commit", "-q", "-m", "docs")
    for i in range(_FIXTURE_COMMITS):
        (repo / f"f{i % 5}.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"c{i}")
    return repo


@pytest.fixture(scope="module")
def engine() -> WarmEngine:
    if not IS_WINDOWS:
        pytest.skip("engine overhead gate is Windows-only: job-object accounting exists only there")
    if not (_ENTRY_DOOR.exists() and _PING_DOOR.exists()):
        pytest.skip(f"{_ENTRY_DOOR} / {_PING_DOOR} not installed on this box")
    with isolated_warm_engine(_source_root(), entrypoints=[ENTRYPOINT, "coordinator-invoke"]) as warm:
        yield warm


def _door_call(cmd: list[str], env: dict, *, must_succeed: bool = True) -> None:
    done = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=_CALL_TIMEOUT_S,
        **no_console_creationflags(),
    )
    assert not must_succeed or done.returncode == 0, f"door call rc={done.returncode}: {done.stderr[:300]!r}"


def _pool_size() -> int:
    from coordinator_core.warm.server import DISPATCH_PROCESS_POOL_SIZE

    return int(DISPATCH_PROCESS_POOL_SIZE)


def _live_children(pid: int) -> int:
    import psutil

    return len([c for c in psutil.Process(pid).children(recursive=False) if "conhost" not in c.name().lower()])


def _fill_pool(acct: LiveTreeAccountant, warm: WarmEngine, cmd: list[str]) -> dict:
    """Concurrent calls overflow the pipe (ERROR_PIPE_BUSY) and run cold outside the engine, so a
    fill call's own exit code says nothing about the engine; the counted calls assert theirs."""
    size = _pool_size()
    before = acct.snapshot()["procs"]
    for _ in range(_POOL_FILL_ROUNDS):
        with ThreadPoolExecutor(max_workers=size) as ex:
            list(ex.map(lambda _i: _door_call(cmd, warm.env, must_succeed=False), range(size)))
        if _live_children(warm.pid) >= size:
            break
    return {"warmup_procs": acct.snapshot()["procs"] - before, "children": _live_children(warm.pid), "pool": size}


def _settle(acct: LiveTreeAccountant) -> float:
    """Block until the freshly filled pool has finished its preload CPU: two consecutive
    _SETTLE_WINDOW_S windows each under _SETTLE_MS_PER_S. Returns the seconds waited. The idle
    engine's steady tax (accept process, resident workers) sits well under the threshold, so a
    periodic in-steady-state cost is never settled away."""
    start = time.monotonic()
    calm = 0
    prev = acct.snapshot()["process_time_ms"]
    while calm < 2 and time.monotonic() - start < _SETTLE_TIMEOUT_S:
        time.sleep(_SETTLE_WINDOW_S)
        now = acct.snapshot()["process_time_ms"]
        calm = calm + 1 if (now - prev) / _SETTLE_WINDOW_S < _SETTLE_MS_PER_S else 0
        prev = now
    return time.monotonic() - start


def _measure(
    probe: str, warm: WarmEngine, cmd: list[str], driver_argv: list[str], accounting: LiveTreeAccountant,
) -> OverheadSample:
    fill = _fill_pool(accounting, warm, cmd)
    fill["settle_secs"] = round(_settle(accounting), 1)
    assert fill["settle_secs"] < _SETTLE_TIMEOUT_S, f"{probe}: pool never settled: {fill}"
    print(f"ENGINE-OP-OVERHEAD-WARMUP {probe}: {fill} (boot cost, not asserted)")
    for _ in range(K_CALLS):
        _door_call(cmd, warm.env)
    warm_ms, warm_procs = [], []
    for _ in range(SAMPLES):
        before = accounting.snapshot()
        for _call in range(K_CALLS):
            _door_call(cmd, warm.env)
        after = accounting.snapshot()
        warm_ms.append((after["process_time_ms"] - before["process_time_ms"]) / K_CALLS)
        warm_procs.append((after["procs"] - before["procs"]) / K_CALLS)
    in_ms, in_procs = in_process_sample_ms(driver_argv, k=K_CALLS)
    sample = OverheadSample(
        probe, statistics.fmean(warm_ms), in_ms, statistics.fmean(warm_procs), in_procs
    )
    print(
        f"ENGINE-OP-OVERHEAD {probe}: warm_op_ms={sample.warm_op_ms:.3f} "
        f"in_process_ms={sample.in_process_ms:.3f} overhead_ms={sample.overhead_ms:.3f} "
        f"warm_procs={sample.warm_procs:.3f} in_process_procs={sample.in_process_procs:.3f} "
        f"unexplained_procs={sample.unexplained_procs:.3f} samples={SAMPLES} k={K_CALLS} "
        f"warm_ms_samples={[round(x, 3) for x in warm_ms]}"
    )
    return sample


@pytest.fixture(scope="module")
def accountant(engine) -> LiveTreeAccountant:
    """Attached before any dispatch: job membership is fixed at process creation."""
    with LiveTreeAccountant(engine.pid) as acct:
        yield acct


def test_ping_overhead_is_under_the_bar(engine, accountant):
    sample = _measure(
        "ping", engine, [str(_PING_DOOR), "ping", "{}"],
        [sys.executable, "-c", _driver(_PING_DRIVER)], accountant,
    )
    assert over_budget(sample) is None, over_budget(sample)


def test_doc_staleness_overhead_is_under_the_bar(engine, accountant, staleness_repo):
    sample = _measure(
        "doc-staleness", engine, [str(_ENTRY_DOOR), "--root", str(staleness_repo)],
        [sys.executable, "-c", _driver(_DOC_STALENESS_DRIVER), str(staleness_repo)], accountant,
    )
    assert over_budget(sample) is None, over_budget(sample)
