"""The prefire launch-lookup falsifier reports zero temp-dir listings once the lookup avoids them."""
import os

import pytest

from coordinator_core.benchmarks import prefire_launch_lookup_cost as cost
from pathlib import Path

_SCRIPT = os.path.join(os.path.dirname(cost.__file__), "prefire_launch_lookup_cost.py")


def test_probe_finds_the_launch_records(tmp_path: Path) -> None:
    result = cost.measure(20, str(tmp_path), repeats=1)
    assert result["engine_ids"] == cost.LAUNCH_RECORDS
