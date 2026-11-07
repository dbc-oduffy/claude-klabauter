"""The scoped-pytest bin launcher answers --help and runs one node id from a foreign cwd."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

import subprocess
import sys
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "scoped-pytest.py"


def test_help_exits_zero():
    r = subprocess.run([sys.executable, str(LAUNCHER), "--help"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert "--allow" in r.stdout


def test_target_outside_allow_is_refused_from_foreign_cwd(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_x():\n    assert True\n")
    r = subprocess.run(
        [sys.executable, str(LAUNCHER), "--allow", "test_a.py", "--", "test_b.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 64


def test_runs_one_node_from_foreign_cwd(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_x():\n    assert True\n\ndef test_y():\n    assert False\n")
    r = subprocess.run(
        [sys.executable, str(LAUNCHER), "--allow", "test_a.py", "--", "test_a.py::test_x", "-q"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout
