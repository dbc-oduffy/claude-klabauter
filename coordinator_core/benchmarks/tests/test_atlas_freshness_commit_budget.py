"""Falsifier for folding atlas drift repair into `git_native.commit_scoped`.

Measures process time and spawn count of one `commit_scoped` call committing a new file an atlas
system page covers, on a fixture repo carrying `docs/architecture/file-index.md` and one system
page. Same instrument as `test_commit_path_process_budget.py` (`batched_process_time_ms`,
process time, never wall clock). The driver's interpreter start, imports and file write are
calibrated separately (same driver, no commit) and subtracted.

The ceilings are the brightline (500ms) and the spawn count calibrated at base before any fold
lands; a fold that adds a spawn or crosses the bar fails here.
"""

from __future__ import annotations

import inspect
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
from coordinator_core.cartography import atlas_freshness

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 8
N_TRACKED_FILES = 60
N_HISTORY_REVISIONS = 2

COMMIT_PROCESS_TIME_BAR_MS = 500.0
"""DR-344 brightline."""

BASE_SPAWN_COUNT = 1.0
"""Spawns per `commit_scoped` call at base (residue excluded). Measured at base a55a90b0ab on
macOS Darwin, k=8, three runs: 1.0 procs each, 50.3-55.8ms process time."""
SPAWN_COUNT_HEADROOM = 0.5
"""Spawn counts are near-deterministic; headroom covers one retry-shaped variance, never a spawn."""

_CLAUDE_KLABAUTER_ROOT = str(Path(__file__).resolve().parents[2].parent)
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_FILE_INDEX = """\
---
last_mapped: 2026-08-06
---

# file-index

## Directory → system

### The remaining systems

| System | Directories | Files | Lines |
|---|---|---|---|
| ceremony-ops | `coordinator_core/ops/ceremony/` | 1 | 1 |
"""

_PAGE = """\
---
system: ceremony-ops
last_mapped: 2026-08-06
files: 1
lines: 1
---

# ceremony-ops
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NO_WINDOW
    )


def _build_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/machine-a/c1-freshness-fixture")
    _git(repo, "config", "user.email", "c1-freshness@example.com")
    _git(repo, "config", "user.name", "c1-freshness")
    arch = repo / "docs" / "architecture"
    (arch / "systems").mkdir(parents=True)
    (arch / "file-index.md").write_text(_FILE_INDEX, encoding="utf-8")
    (arch / "systems" / "ceremony-ops.md").write_text(_PAGE, encoding="utf-8")
    ceremony = repo / "coordinator_core" / "ops" / "ceremony"
    ceremony.mkdir(parents=True)
    for rev in range(N_HISTORY_REVISIONS):
        for i in range(N_TRACKED_FILES):
            (ceremony / f"m{i:03d}.py").write_text(f"# rev {rev} {i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"history revision {rev}")
    return repo


def _write_driver(driver: Path, repo: Path, counter: Path, do_commit: bool) -> None:
    """Per-invocation wrapper: writes one NEW covered file (fresh name per call, so each of the k
    re-runs commits an addition), then calls `commit_scoped` when `do_commit`."""
    commit = (
        "res = commit_scoped([rel], str(msg), str(repo))\n"
        "assert res.ok, res.stderr\n"
        if do_commit
        else ""
    )
    driver.write_text(
        f'''\
import sys
from pathlib import Path

sys.path.insert(0, r"{_CLAUDE_KLABAUTER_ROOT}")
from coordinator_core.ops.ceremony.git_native import commit_scoped

repo = Path(r"{repo}")
counter = Path(r"{counter}")
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
rel = f"coordinator_core/ops/ceremony/new{{n:04d}}.py"
(repo / rel).write_text(f"# new {{n}}\\n", encoding="utf-8")
msg = counter.with_name("msg.txt")
msg.write_text(f"add new file {{n}}\\n", encoding="utf-8")
{commit}''',
        encoding="utf-8",
    )


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("COORDINATOR_ENGINE_ROOT", _CLAUDE_KLABAUTER_ROOT)
    return env


@pytest.fixture(scope="module")
def commit_scoped_measurement(tmp_path_factory):
    if not (IS_WINDOWS or IS_DARWIN):
        pytest.skip("process-time accounting primitive exists on Windows and Darwin only")
    tmp_path = tmp_path_factory.mktemp("atlas_freshness_budget")
    repo = _build_fixture_repo(tmp_path)
    env = _env()

    residue_driver = tmp_path / "driver_residue.py"
    _write_driver(residue_driver, repo, tmp_path / "residue_counter.txt", do_commit=False)
    residue = batched_process_time_ms(
        [sys.executable, str(residue_driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert residue["rc"] == 0, f"residue calibration must exit 0: {residue!r}"
    _git(repo, "clean", "-fdq")

    full_driver = tmp_path / "driver_commit.py"
    commit_counter = tmp_path / "commit_counter.txt"
    _write_driver(full_driver, repo, commit_counter, do_commit=True)
    full = batched_process_time_ms(
        [sys.executable, str(full_driver)], k=K_INVOCATIONS, cwd=str(repo), env=env
    )
    assert full["rc"] == 0, f"commit_scoped measurement must exit 0: {full!r}"

    committed = subprocess.run(
        ["git", "log", "--name-status", "--format=", f"-{K_INVOCATIONS}"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    ).stdout.splitlines()
    added = [ln for ln in committed if ln.startswith("A\t") and "/new" in ln]
    assert len(added) == K_INVOCATIONS, (
        f"each of {K_INVOCATIONS} invocations must commit one added covered file; "
        f"saw {added!r} -- a commit that did not land is not a measurement"
    )

    ms = full["process_time_ms"] - residue["process_time_ms"]
    procs = full["procs_per_call"] - residue["procs_per_call"]
    return {
        "ms": ms,
        "procs": procs,
        "detail": (
            f"full {full['process_time_ms']}ms/{full['procs_per_call']} procs, "
            f"residue {residue['process_time_ms']}ms/{residue['procs_per_call']} procs "
            f"(k={K_INVOCATIONS}); commit_scoped {ms}ms/{procs} procs"
        ),
    }


def test_commit_scoped_stays_inside_the_brightline(commit_scoped_measurement):
    m = commit_scoped_measurement
    assert m["ms"] <= COMMIT_PROCESS_TIME_BAR_MS, (
        f"commit_scoped over the {COMMIT_PROCESS_TIME_BAR_MS}ms bar: {m['detail']}"
    )


def test_commit_scoped_spawn_count_is_unchanged_from_base(commit_scoped_measurement):
    m = commit_scoped_measurement
    ceiling = BASE_SPAWN_COUNT + SPAWN_COUNT_HEADROOM
    assert m["procs"] <= ceiling, (
        f"commit_scoped spawns exceed base {BASE_SPAWN_COUNT} (+{SPAWN_COUNT_HEADROOM}): "
        f"{m['detail']}"
    )


def test_interface_shape():
    assert [f for f in atlas_freshness.RepairPlan.__dataclass_fields__] == [
        "page_relpath",
        "files_delta",
    ]
    assert list(atlas_freshness.ActedFact.__dataclass_fields__) == [
        "page",
        "sha",
        "kind",
        "detail",
        "at",
    ]
    assert list(inspect.signature(atlas_freshness.plan_repair).parameters) == [
        "root",
        "paths",
        "deleted_paths",
        "added_paths",
    ]
    assert atlas_freshness.ACTED_LEDGER_RELPATH == ".coordinator-local/freshness/atlas-acted.jsonl"
