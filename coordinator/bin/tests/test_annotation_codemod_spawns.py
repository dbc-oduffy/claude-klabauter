"""Tests for coordinator/bin/annotation-codemod.py: rewrite rules, refusals, idempotence, --check."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parent.parent / "annotation-codemod.py"
_spec = importlib.util.spec_from_file_location("annotation_codemod", _SCRIPT)
assert _spec is not None and _spec.loader is not None
codemod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codemod)


def test_cli_entrypoint_exit_code(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def f():\n    print(1)\n")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", str(f)],
        capture_output=True, text=True, creationflags=flags,
    )
    assert proc.returncode == 1
    assert "missing: 1 in 1 files" in proc.stdout
    subprocess.run([sys.executable, str(_SCRIPT), str(f)], check=True, creationflags=flags)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--check", str(f)],
        capture_output=True, text=True, creationflags=flags,
    )
    assert proc.returncode == 0
