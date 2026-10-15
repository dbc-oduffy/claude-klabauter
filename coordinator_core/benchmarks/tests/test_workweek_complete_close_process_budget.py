"""Standing gate: the workweek-complete archive close under the 500ms bar, by tree process time.

The fixture is a `git init` repo carrying this tree's `prepare-commit-msg` hook, seeded with the
weekly-scale file counts (S2's fixture B: 15 changelog files, 35 review-trail records, named
constants below) and committed, so the archive's moves are real deletes plus adds. It is built and
copied once; each sample restores a fresh copy outside the measured call. The measured command is
`workweek-complete-close.py archive ... --no-push`: the push is a remote round trip and the bar
does not govern it (CLAUDE.md, The brightline, Scope).

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 500ms is the brightline bar itself (DR-344); procs_per_call < 3 is the
exit criterion for this step. STATISTIC: the median of the samples, not the min.

The commit tail runs on `run_close_commit` (zero git spawns). The WORKWEEK-CLOSE-ARCHIVE-BUDGET
line this gate prints carries the live figure.

Fixture C (15 changelog files, 1563 review-trail records) is excluded: it is the gravestoned
review-trail writer's historical volume (DR-372, DR-374), not the step's weekly scale, and the
moves alone cost 1593.8ms on it (S2).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SAMPLES = 5
BUDGET_MS = 500.0
MAX_PROCS = 3

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[3]
_BIN_DIR = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin"
_CLOSE_CLI = _BIN_DIR / "workweek-complete-close.py"
_WEEK_STARTING = "2026-08-03"
_CHANGELOG_DAILY = 7
_CHANGELOG_BACKFILL = 6
_CHANGELOG_PRIORITIES = 1
_CHANGELOG_FILES = 1 + _CHANGELOG_DAILY + _CHANGELOG_BACKFILL + _CHANGELOG_PRIORITIES
_TRAIL_FILES = 35
_SESSION_ID = "00000000-0000-4000-8000-000000000042"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _git(root: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, **no_console_creationflags(),
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"


def _install_hook(root: Path) -> None:
    spec = importlib.util.spec_from_file_location("ghi_for_close_gate", _BIN_DIR / "lib" / "git_hook_install.py")
    ghi = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_BIN_DIR / "lib"))
    try:
        spec.loader.exec_module(ghi)
        rc = ghi.ensure_prepare_commit_msg_hook(str(_BIN_DIR), root=str(root))
    finally:
        sys.path.remove(str(_BIN_DIR / "lib"))
    assert rc == 0, f"prepare-commit-msg hook install rc {rc}"
    assert (root / ".git" / "hooks" / "prepare-commit-msg").is_file(), "hook not installed in the fixture"


def _build_fixture(root: Path) -> dict:
    week = root / "state" / "week-changelog"

    _write(week / "HEADER.md", f"**Week starting:** {_WEEK_STARTING}\n")
    for d in range(_CHANGELOG_DAILY):
        _write(week / f"2026-08-{3 + d:02d}.md", f"day {d}\n")
        if d < _CHANGELOG_BACKFILL:
            _write(week / f"2026-08-{3 + d:02d}-backfill.md", f"backfill {d}\n")
    _write(week / f"HEADER.priorities.{_WEEK_STARTING}.md", "priorities\n")
    for i in range(_TRAIL_FILES):
        _write(
            root / "state" / "review-trail" / f"2026-08-{3 + i % 7:02d}-{i // 60:02d}{i % 60:02d}00-4839fcc4-{i}.json",
            "{}\n",
        )
    _write(root / "archive" / "week-changelogs" / ".keep", "")
    _write(root / "archive" / "review-trail" / ".keep", "")

    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "fixture")
    _install_hook(root)
    return {"changelog_files": _CHANGELOG_FILES, "review_trail_files": _TRAIL_FILES}


@pytest.fixture(scope="module")
def archive_measurement(tmp_path_factory):
    base = tmp_path_factory.mktemp("workweek_close_budget")
    pristine = base / "pristine"
    pristine.mkdir()
    scale = _build_fixture(pristine)

    env = {
        **os.environ,
        "COORDINATOR_SESSION_ID": _SESSION_ID,
        "COORDINATOR_ENGINE_ROOT": str(_CLAUDE_KLABAUTER_ROOT),
    }
    samples = []
    try:
        for n in range(SAMPLES):
            work = base / f"work{n}"
            shutil.copytree(pristine, work)
            argv = [
                sys.executable, str(_CLOSE_CLI), "archive", "--repo-root", str(work),
                "--version", "v0.0.0", "--merge-sha", "deadbeef", "--week-starting", _WEEK_STARTING,
                "--no-push",
            ]
            samples.append(batched_process_time_ms(argv, k=1, env=env, cwd=str(work)))
    except NotImplementedError as exc:
        pytest.skip(f"process-time primitive unsupported here: {exc}")
    for s in samples:
        assert s["rc"] == 0, f"archive exited {s['rc']}: {s!r}"

    median_ms = round(statistics.median(s["process_time_ms"] for s in samples), 3)
    median_procs = statistics.median(s["procs_per_call"] for s in samples)
    detail = (
        f"workweek-complete archive close (--no-push), median tree process time: {median_ms}ms, "
        f"procs_per_call: {median_procs} (k=1, median of {SAMPLES}; fixture {scale})"
    )
    print(f"WORKWEEK-CLOSE-ARCHIVE-BUDGET {detail}")
    return {"ms": median_ms, "procs": median_procs, "detail": detail}


def test_workweek_close_archive_is_under_the_500ms_bar(archive_measurement):
    m = archive_measurement
    assert m["ms"] > 0, f"archive cost not observed: {m['detail']}"
    assert m["ms"] <= BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_workweek_close_archive_spawns_fewer_than_three_processes(archive_measurement):
    m = archive_measurement
    assert m["procs"] < MAX_PROCS, f"procs_per_call not under {MAX_PROCS}: {m['detail']}"
