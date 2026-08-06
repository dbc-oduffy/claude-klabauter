"""Regression pin — bare-file cmdline arg orphans a Package's own conftest.

Spec backlink: the `_matchfactories` patch in coordinator_core/conftest.py
(§ "Package-conftest visibility patch (2026-07-28 — bare-file-arg Package-
cache clobber)"), which this test exists to keep green. Read that docstring
first — it has the full mechanism writeup; this file only pins the symptom.

WHAT THIS PINS. A specific 3-arg pytest invocation that used to fail with
`fixture 'handoff_repo' not found` for tests in
coordinator_core/ops/tests/test_handoff_archive_transition.py, purely because
of WHICH other files were named alongside it and in WHAT ORDER — no source
change to that file, its conftest, or the fixture itself. Runs pytest as a
subprocess (not in-process) because the bug is in `_pytest.main.Session`
collection-cache state, which is process-global; an in-process repro would
observe THIS test process's own already-corrupted or already-clean cache
depending on collection order, not a fresh one.

NEGATIVE SPEC
    - Deliberately does NOT assert on total pass/fail counts beyond "zero
      errors" — the underlying suites evolve independently and a hardcoded
      count would go stale and mask unrelated failures as this bug recurring.
    - Deliberately re-runs the ORIGINAL 3-file combination from the incident,
      not a synthetic minimal case, so a regression in either the conftest
      patch OR an unrelated change to one of the three named files that
      happens to reintroduce the trigger shape is caught by the same test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bare_baton_assemble_arg_does_not_orphan_ops_tests_conftest() -> None:
    """The exact 3-arg repro from the 2026-07-28 incident must collect and
    run every test cleanly — zero setup errors, in particular none of the
    `fixture 'handoff_repo' not found` shape this test is named for."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "coordinator_core/ops/tests/test_handoff_reconcile_report.py",
            "coordinator_core/test_baton_assemble.py",
            "coordinator_core/ops/tests/test_handoff_archive_transition.py",
            "-q",
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    combined = result.stdout + result.stderr
    assert "fixture 'handoff_repo' not found" not in combined, (
        "the bare-file-arg Package-cache clobber regressed — see "
        "coordinator_core/conftest.py's _matchfactories patch docstring.\n\n"
        f"{combined}"
    )
    assert "errors" not in combined.lower() or "0 errors" in combined.lower(), (
        f"unexpected setup errors in the 3-arg repro:\n\n{combined}"
    )
    assert result.returncode == 0, f"repro invocation failed:\n\n{combined}"
