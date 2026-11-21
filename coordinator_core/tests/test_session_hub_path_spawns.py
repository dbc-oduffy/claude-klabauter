"""Pins coordinator_core.session_hub_path: hub placement and the leaf-import contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_module_is_a_leaf():
    code = (
        "import sys; import coordinator_core.session_hub_path;"
        "sys.exit(1 if 'coordinator_core.session.core' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).parents[2])).returncode == 0
