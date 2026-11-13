"""Falsifier for `atlas_catchup.catch_up`: process time and spawn count at steady state (HEAD ==
watermark) and after one out-of-engine commit adding a covered file.

Same instrument as `test_atlas_freshness_commit_budget.py` (`batched_process_time_ms`, process
time, never wall clock). Each leg is measured against a residue driver that does the same setup
(interpreter start, imports, and for the moved leg the `git add` + `git commit`) without calling
`catch_up`; the difference is `catch_up` alone.

Ceilings: steady state 0 spawns and < 50ms; moved HEAD one spawn (the single `git log`) and the
500ms brightline (DR-344).
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)
from coordinator_core.benchmarks.tests.test_atlas_freshness_commit_budget import (
    _CLAUDE_KLABAUTER_ROOT,
    _NO_WINDOW,
    _build_fixture_repo,
    _env,
    _git,
)
from coordinator_core.cartography import atlas_catchup

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 8

STEADY_PROCESS_TIME_BAR_MS = 50.0
MOVED_PROCESS_TIME_BAR_MS = 500.0
"""DR-344 brightline."""
STEADY_SPAWN_CEILING = 0.0
MOVED_SPAWN_CEILING = 1.0
SPAWN_COUNT_HEADROOM = 0.5
"""Spawn counts are near-deterministic; headroom covers measurement noise, never a spawn."""

_PAGE_RELPATH = "docs/architecture/systems/ceremony-ops.md"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    ).stdout.strip()


def _seed_watermark(repo: Path) -> None:
    path = repo / atlas_catchup.WATERMARK_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sha": _head(repo), "folded": []}), encoding="utf-8")


def _write_driver(driver: Path, repo: Path, counter: Path, moved: bool, do_catchup: bool) -> None:
    """Per-invocation wrapper. `moved`: commit one NEW covered file with plain git first (fresh
    name per call). `do_catchup`: then call `catch_up` and assert it did the leg's expected work."""
    commit = (
        "import subprocess\n"
        "n = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(n))\n"
        f'rel = f"coordinator_core/ops/ceremony/{counter.stem}_{{n:04d}}.py"\n'
        '(repo / rel).write_text(f"# new {n}\\n", encoding="utf-8")\n'
        'subprocess.run(["git", "add", rel], cwd=str(repo), check=True, capture_output=True)\n'
        'subprocess.run(["git", "commit", "-q", "-m", f"add {n}"], cwd=str(repo), check=True, capture_output=True)\n'
        if moved
        else ""
    )
    if do_catchup:
        expect = (
            "assert res.ran and res.spawns == 1, res\n"
            if moved
            else "assert not res.ran and res.spawns == 0, res\n"
        )
        call = f"res = catch_up(repo)\n{expect}"
    else:
        call = ""
    driver.write_text(
        f'''\
import sys
from pathlib import Path

sys.path.insert(0, r"{_CLAUDE_KLABAUTER_ROOT}")
from coordinator_core.cartography.atlas_catchup import catch_up

repo = Path(r"{repo}")
counter = Path(r"{counter}")
{commit}{call}''',
        encoding="utf-8",
    )


def _measure(tmp_path: Path, repo: Path, name: str, moved: bool) -> dict:
    env = _env()
    residue_driver = tmp_path / f"driver_{name}_residue.py"
    _write_driver(residue_driver, repo, tmp_path / f"{name}_residue_counter.txt", moved, False)
    residue = batched_process_time_ms(
        [sys.executable, str(residue_driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert residue["rc"] == 0, f"{name} residue calibration must exit 0: {residue!r}"
    _seed_watermark(repo)

    full_driver = tmp_path / f"driver_{name}.py"
    _write_driver(full_driver, repo, tmp_path / f"{name}_counter.txt", moved, True)
    full = batched_process_time_ms(
        [sys.executable, str(full_driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert full["rc"] == 0, f"{name} catch_up measurement must exit 0: {full!r}"

    ms = full["process_time_ms"] - residue["process_time_ms"]
    procs = full["procs_per_call"] - residue["procs_per_call"]
    return {
        "ms": ms,
        "procs": procs,
        "detail": (
            f"{name}: full {full['process_time_ms']}ms/{full['procs_per_call']} procs, "
            f"residue {residue['process_time_ms']}ms/{residue['procs_per_call']} procs "
            f"(k={K_INVOCATIONS}); catch_up {ms}ms/{procs} procs"
        ),
    }


@pytest.fixture(scope="module")
def measurements(tmp_path_factory):
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip("process-time accounting primitive exists on Windows and Darwin only")
    tmp_path = tmp_path_factory.mktemp("atlas_catchup_budget")
    repo = _build_fixture_repo(tmp_path)
    _git(repo, "config", "gc.auto", "0")
    _seed_watermark(repo)
    steady = _measure(tmp_path, repo, "steady", moved=False)

    moved = _measure(tmp_path, repo, "moved", moved=True)
    pending = json.loads((repo / atlas_catchup.PENDING_RELPATH).read_text(encoding="utf-8"))
    assert pending.get(_PAGE_RELPATH, 0) >= K_INVOCATIONS, (
        f"each moved-HEAD run must queue a +1 for {_PAGE_RELPATH}; saw {pending!r} -- "
        "a catch-up that queued nothing is not a measurement"
    )
    return {"steady": steady, "moved": moved}


def test_steady_state_is_free(measurements):
    m = measurements["steady"]
    assert m["procs"] <= STEADY_SPAWN_CEILING + SPAWN_COUNT_HEADROOM, (
        f"steady-state catch_up spawned: {m['detail']}"
    )
    assert m["ms"] < STEADY_PROCESS_TIME_BAR_MS, (
        f"steady-state catch_up over {STEADY_PROCESS_TIME_BAR_MS}ms: {m['detail']}"
    )


def test_moved_head_stays_inside_the_brightline(measurements):
    m = measurements["moved"]
    assert m["ms"] <= MOVED_PROCESS_TIME_BAR_MS, (
        f"moved-HEAD catch_up over the {MOVED_PROCESS_TIME_BAR_MS}ms bar: {m['detail']}"
    )


def test_moved_head_runs_one_spawn(measurements):
    m = measurements["moved"]
    assert m["procs"] <= MOVED_SPAWN_CEILING + SPAWN_COUNT_HEADROOM, (
        f"moved-HEAD catch_up exceeds one spawn: {m['detail']}"
    )


def test_interface_shape():
    assert list(atlas_catchup.CatchUpResult.__dataclass_fields__) == [
        "ran",
        "spawns",
        "facts",
        "pending",
        "reason",
    ]
    assert atlas_catchup.CatchUpResult.__dataclass_params__.frozen
    assert list(inspect.signature(atlas_catchup.catch_up).parameters) == ["repo"]
    assert list(inspect.signature(atlas_catchup.on_idle_tick).parameters) == ["repos"]
    assert list(inspect.signature(atlas_catchup.read_pending).parameters) == ["repo"]
    assert list(inspect.signature(atlas_catchup.settle_commit).parameters) == [
        "repo",
        "applied_pages",
        "sha",
        "parent_sha",
    ]
    assert atlas_catchup.WATERMARK_RELPATH == ".coordinator-local/freshness/atlas-watermark.json"
    assert atlas_catchup.PENDING_RELPATH == ".coordinator-local/freshness/atlas-pending.json"
    assert atlas_catchup.CATCHUP_RANGE_CAP > 0 and atlas_catchup.CATCHUP_INTERVAL_SECS > 0
