"""Recurrence guard: a warm test-suite run must create ZERO new directories
under the operator's real `%LOCALAPPDATA%\\coordinator\\warm\\`.

Spec backlink: state/handoffs/2026-08-20-warm-breadcrumbs-litter-the-operators-runtime-base.md

WHY THIS IS A SUBPROCESS RUN AND NOT AN IN-PROCESS ASSERTION
    The defect being guarded is a property of a whole pytest RUN: the warm
    tests pass a `tmp_path` as `engine_root`, which varies only `svc_dir`'s
    clone-hash component while the base stays the operator's real one, so
    each run minted a brand-new REAL directory per test and populated it
    with breadcrumb and telemetry fixtures nothing ever removed (measured
    2026-08-20 on the authoring box: 1027 clone-key directories, 412 with a
    `warm.json`, 244 holding obviously synthetic fixture content). Asserting
    in-process would test this process's own env, which the suite-wide
    quarantine has already redirected -- it could not observe the failure it
    exists to catch. Counting the real base across a child run can.

    The child deliberately runs with `COORDINATOR_WARM_RUNTIME_BASE`
    REMOVED from its environment, so it re-derives isolation the way a real
    suite run does -- through `coordinator_core/conftest.py`'s
    `_quarantine_real_home` -- rather than inheriting this process's
    already-quarantined value. Inheriting it would make the guard pass on a
    tree where the fixture had been deleted.

Reads the real base, never writes it: a failure here reports a count, and
the cleanup of any litter it finds is the operator's, not this test's.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The writer-heaviest modules: every warm test that calls `write_breadcrumb`,
# `telemetry.flush`, or `supervisor.write_discovery` lives in one of these.
_WRITER_MODULES = (
    "coordinator_core/warm/tests/test_breadcrumb_and_spawn.py",
    "coordinator_core/warm/tests/test_warm_telemetry.py",
    "coordinator_core/warm/tests/test_supervisor.py",
)


def _real_warm_base() -> Path:
    """`<real LOCALAPPDATA>/coordinator/warm` -- resolved WITHOUT the test
    override, which is exactly the directory the defect littered."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".cache"
    return base / "coordinator" / "warm"


def _clone_key_dirs(base: Path) -> set[str]:
    if not base.is_dir():
        return set()
    return {entry.name for entry in base.iterdir() if entry.is_dir()}


def test_a_warm_suite_run_creates_no_new_real_runtime_directories() -> None:
    real_base = _real_warm_base()
    before = _clone_key_dirs(real_base)

    child_env = dict(os.environ)
    child_env.pop(breadcrumb.RUNTIME_BASE_ENV, None)
    # A child pytest that recursed back into this module would spawn its own
    # child, and so on. Deselect this file in the child run.
    child_env["PYTEST_ADDOPTS"] = ""

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            *_WRITER_MODULES,
        ],
        cwd=str(_REPO_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
        timeout=900,
    )

    after = _clone_key_dirs(real_base)
    minted = sorted(after - before)

    assert not minted, (
        f"a warm test-suite run minted {len(minted)} new directory(ies) under the "
        f"operator's real runtime base {real_base}: {minted[:10]}. The test seam "
        f"({breadcrumb.RUNTIME_BASE_ENV}, applied by coordinator_core/conftest.py's "
        "_quarantine_real_home) is not reaching every warm writer."
    )
    assert completed.returncode == 0, (
        "the child warm-test run failed, so its zero-litter result proves nothing:\n"
        f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
    )
