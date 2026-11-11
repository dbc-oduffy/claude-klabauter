"""A green pytest run exits 0 without an assert-rewrite warning; a forced non-zero exit names its cause."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(argv, cwd):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *argv],
        cwd=cwd, capture_output=True, text=True, timeout=120, creationflags=_FLAGS,
    )


def test_green_run_under_repo_conftest_exits_zero_without_rewrite_warning():
    proc = _run(["coordinator_core/testing/tests/test_tier_collect_skip.py"], REPO)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-2000:]
    assert "PytestAssertRewriteWarning" not in out, out[-2000:]
    assert "[forced-exit]" not in out, out[-2000:]


def test_forced_exit_names_its_cause_under_q(tmp_path):
    (tmp_path / "forcer.py").write_text(
        "from coordinator_core.testing.fs_containment import force_exit_status\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    force_exit_status(session, 1, 'synthetic cause xyzzy')\n",
        encoding="utf-8",
    )
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "forcer", "test_ok.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120, creationflags=_FLAGS,
        env={**os.environ, "PYTHONPATH": str(tmp_path) + os.pathsep + str(REPO)},
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out[-2000:]
    assert "[forced-exit] rc=1: synthetic cause xyzzy" in out, out[-2000:]
    assert "1 passed" in out, out[-2000:]


def test_clean_inner_run_without_forcer_exits_zero(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    proc = _run(["test_ok.py"], tmp_path)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
