"""Falsifier for one default-param `fleet.authoring_checkout_freshness` call over 6 checkouts.

Fixture: a bare origin plus 6 clones on a day branch -- 4 current, 1 behind-clean (fast-forwarded
on every call), 1 diverged (merge-tree probe). Same instrument as
`test_atlas_freshness_commit_budget.py` (`batched_process_time_ms`, process time, never wall
clock). The driver's interpreter start, imports and per-call fixture reset (rewinding the behind
checkout) run identically in a calibration leg that skips the op call and are subtracted.

Spawn ceiling: 2 per checkout (status, rev-list) x 6, plus 1 merge-tree and the ff-only merge
(3 processes with its children) = 16.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_DARWIN,
    IS_WINDOWS,
    batched_process_time_ms,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 8
N_CHECKOUTS = 6

FRESHNESS_PROCESS_TIME_BAR_MS = 500.0
"""DR-344 brightline."""

BASE_SPAWN_COUNT = 16.0
"""Spawns per op call (residue excluded): 2*6 probes + 1 merge-tree + 3 for `merge --ff-only`
(git itself plus two children it spawns; the op issues 14 git commands). Measured at base
3dfa9a5f48 + the C0-C2 rows on macOS Darwin, k=8: 16.0 procs, ~116ms process time per call."""
SPAWN_COUNT_HEADROOM = 0.5
"""Spawn counts are near-deterministic; headroom covers one retry-shaped variance, never a spawn."""

_CLAUDE_KLABAUTER_ROOT = str(Path(__file__).resolve().parents[2].parent)
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NO_WINDOW
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)


def _build_fixture(tmp_path: Path) -> tuple[Path, dict[str, Path], str]:
    """Returns `(settings_home, {registry key: clone}, base sha the behind clone rewinds to)`."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    for repo in (seed,):
        _git(repo, "config", "user.email", "bench@example.com")
        _git(repo, "config", "user.name", "bench")
    _git(seed, "checkout", "-q", "-b", "main")
    _commit(seed, "base.txt")
    _git(seed, "push", "-q", "origin", "main")
    base_sha = _git(seed, "rev-parse", "HEAD")

    clones: dict[str, Path] = {}
    for i in range(N_CHECKOUTS):
        clone = tmp_path / f"co{i}"
        _git(tmp_path, "clone", "-q", str(origin), str(clone))
        _git(clone, "config", "user.email", "bench@example.com")
        _git(clone, "config", "user.name", "bench")
        _git(clone, "checkout", "-q", "-b", "work/bench/day")
        clones[f"repos.co{i}"] = clone

    _commit(seed, "advance.txt")
    _git(seed, "push", "-q", "origin", "main")
    behind, diverged = clones["repos.co4"], clones["repos.co5"]
    for clone in (behind, diverged):
        _git(clone, "fetch", "-q", "origin", "main")
    _commit(diverged, "local.txt")

    home = tmp_path / "settings-home"
    (home / "machine-local").mkdir(parents=True)
    lines = [f'"{key}" = "{path.as_posix()}"' for key, path in clones.items()]
    (home / "machine-local" / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return home, clones, base_sha


def _write_driver(driver: Path, behind: Path, base_sha: str, do_call: bool, result: Path) -> None:
    """Per invocation: rewind the behind checkout, import everything the op lazy-imports, then
    (when `do_call`) run the op and assert the fixture shape actually exercised ff + merge-tree."""
    call = (
        f'''\
out = run()
by_action = sorted(r["action"] for r in out["rows"])
assert len(out["rows"]) == {N_CHECKOUTS}, out
assert by_action == ["current"] * 4 + ["diverged", "fast_forward"], by_action
assert sum(1 for r in out["rows"] if r["moved"]) == 1, out
Path(r"{result}").write_text(json.dumps(out), encoding="utf-8")
'''
        if do_call
        else ""
    )
    driver.write_text(
        f'''\
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"{_CLAUDE_KLABAUTER_ROOT}")
from coordinator_core.ops.fleet.authoring_checkout_freshness import run
from coordinator_core.install import authoring_checkout_freshness_apply  # noqa: F401
from coordinator_core.ops.fleet import _memo_resolver  # noqa: F401
from coordinator_core.warm import live_runs  # noqa: F401

subprocess.run(
    ["git", "reset", "-q", "--hard", "{base_sha}"],
    cwd=r"{behind}", check=True, capture_output=True,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
)
{call}''',
        encoding="utf-8",
    )


def _env(home: Path) -> dict:
    env = dict(os.environ)
    env.pop("MACHINE_LOCAL_IMPL", None)
    env["COORDINATOR_SETTINGS_HOME"] = str(home)
    env["CLAUDE_HOME"] = str(home)
    env.setdefault("COORDINATOR_ENGINE_ROOT", _CLAUDE_KLABAUTER_ROOT)
    return env


@pytest.fixture(scope="module")
def freshness_measurement(tmp_path_factory):
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip("process-time accounting primitive exists on Windows and Darwin only")
    tmp_path = tmp_path_factory.mktemp("authoring_freshness_budget")
    home, clones, base_sha = _build_fixture(tmp_path)
    behind = clones["repos.co4"]
    env = _env(home)
    result = tmp_path / "result.json"

    residue_driver = tmp_path / "driver_residue.py"
    _write_driver(residue_driver, behind, base_sha, do_call=False, result=result)
    residue = batched_process_time_ms(
        [sys.executable, str(residue_driver)], k=K_INVOCATIONS, cwd=str(tmp_path), env=env
    )
    assert residue["rc"] == 0, f"residue calibration must exit 0: {residue!r}"

    full_driver = tmp_path / "driver_call.py"
    _write_driver(full_driver, behind, base_sha, do_call=True, result=result)
    full = batched_process_time_ms(
        [sys.executable, str(full_driver)], k=K_INVOCATIONS, cwd=str(tmp_path), env=env
    )
    assert full["rc"] == 0, f"op measurement must exit 0 (driver asserts the fixture shape): {full!r}"

    out = json.loads(result.read_text(encoding="utf-8"))
    moved = [r for r in out["rows"] if r["moved"]]
    assert len(moved) == 1 and moved[0]["key"] == "repos.co4", (
        f"the behind clone must fast-forward on the measured call; a call that did not is not a "
        f"measurement: {out!r}"
    )
    assert _git(behind, "rev-parse", "HEAD") == _git(behind, "rev-parse", "origin/main")

    ms = full["process_time_ms"] - residue["process_time_ms"]
    procs = full["procs_per_call"] - residue["procs_per_call"]
    return {
        "ms": ms,
        "procs": procs,
        "detail": (
            f"full {full['process_time_ms']}ms/{full['procs_per_call']} procs, "
            f"residue {residue['process_time_ms']}ms/{residue['procs_per_call']} procs "
            f"(k={K_INVOCATIONS}); op call {ms}ms/{procs} procs over {N_CHECKOUTS} checkouts"
        ),
    }


def test_op_call_stays_inside_the_brightline(freshness_measurement):
    m = freshness_measurement
    assert m["ms"] <= FRESHNESS_PROCESS_TIME_BAR_MS, (
        f"op call over the {FRESHNESS_PROCESS_TIME_BAR_MS}ms bar: {m['detail']}"
    )


def test_op_call_spawn_count_is_the_calibrated_one(freshness_measurement):
    m = freshness_measurement
    ceiling = BASE_SPAWN_COUNT + SPAWN_COUNT_HEADROOM
    assert m["procs"] <= ceiling, (
        f"op spawns exceed calibrated {BASE_SPAWN_COUNT} (+{SPAWN_COUNT_HEADROOM}): {m['detail']}"
    )
