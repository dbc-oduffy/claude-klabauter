"""A process a test spawns through subprocess.Popen is gone after the test's teardown."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_PARENT_SRC = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(120)\n"
)


def test_autouse_fixture_wraps_popen_for_this_test():
    assert subprocess.Popen.__name__ == "_Reaped"
