"""ask_compose: the falsifier, judge and verify briefs require driving the production entry point."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENTRY_POINT = "drive that production entry point end-to-end"
_FOREIGN_CWD = "from a foreign cwd (not the repo root) where cwd matters"
_FIXTURE_ONLY = "exercises only route-authored test fixtures"
_NOT_PROVING = "does not prove the criterion"
_NO_ENV_DUMP = "Never write the environment (os.environ or a child env) to a file or reply"
_PROBE_SCRATCH = "scratch directory, never the target repo, and are deleted before you return"
_CLAUSES = (_ENTRY_POINT, _FOREIGN_CWD, _FIXTURE_ONLY, _NOT_PROVING, _NO_ENV_DUMP, _PROBE_SCRATCH)


_OVERLAY_CMD = "-m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id run-1"


def test_overlay_producer_command_imports_coordinator_core_from_a_consumer_cwd(tmp_path: Path) -> None:
    import os
    import shlex
    import subprocess
    import sys

    cmd = ask_compose.engine_module_command("coordinator_core.ops.dispatch_emit.clean_overlay_verify", str(tmp_path), "--help")
    binding, _, *args = shlex.split(cmd)
    key, value = binding.split("=", 1)
    assert key == "PYTHONPATH"
    result = subprocess.run(
        [sys.executable, *args], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, key: value},
    )
    assert result.returncode == 0, result.stderr
