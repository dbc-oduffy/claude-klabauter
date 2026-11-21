"""The emitted family leg: one family attempt, one claude fallback on a read-only role (never on a write row), both results kept.

Most assertions read the emitted JS text; the fallback-reply tests execute the emitted `_familyRun` in node."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import family_invoke_record, stage_family_binding

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RUN = "run-1"
_OPTED = {"stage_family_binding": stage_family_binding.BINDING_REF}


_NODE = shutil.which("node")
_FAILED_FAMILY = {"family": "codex", "stage": "impl", "exit_code": 1, "output_path": "", "verdict": "FAILED", "blocked_reason": "no stage work"}
_ROW = {"id": "C1", "family": "codex", "stage_role": "impl", "fallback_brief_path": "b.md", "agent_type": "a", "fallback_agent_type": "b", "model": "m"}


_AUTHORED_OK = {"family": "codex", "stage": "write", "exit_code": 0, "output_path": "", "verdict": "OK", "family_authored": True, "vendor_model": "gpt-x"}
_WRITE_ROW = {**_ROW, "stage_role": "write", "writes": ["pkg/a.py"]}


def test_engine_invoker_module_exists_and_exposes_the_row_cli():
    import importlib.util
    import subprocess
    import sys

    assert importlib.util.find_spec(family_invoke_record.INVOKE_MODULE) is not None
    out = subprocess.run(
        [sys.executable, "-m", family_invoke_record.INVOKE_MODULE, "--help"],
        capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0
    for flag in ("--root", "--run-id", "--row", "--family", "--stage", "--prompt", "--writes"):
        assert flag in out.stdout
