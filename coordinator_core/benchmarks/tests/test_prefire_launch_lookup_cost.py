"""The prefire launch-lookup falsifier reports zero temp-dir listings once the lookup avoids them."""
import os
import re
import subprocess
import sys

import pytest

from coordinator_core.benchmarks import prefire_launch_lookup_cost as cost

_SCRIPT = os.path.join(os.path.dirname(cost.__file__), "prefire_launch_lookup_cost.py")


def test_lookup_lists_no_tempdir_entries(tmp_path):
    proc = subprocess.run(
        [sys.executable, _SCRIPT, "--unrelated", "2000", "--repeats", "3", "--workdir", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"listdir_of_tmpdir=(\d+) process_ms=([\d.]+)", proc.stdout)
    assert match, proc.stdout
    assert int(match.group(1)) == 0


def test_probe_finds_the_launch_records(tmp_path):
    result = cost.measure(20, str(tmp_path), repeats=1)
    assert result["engine_ids"] == cost.LAUNCH_RECORDS
