"""archive_stamp keeps asyncio off its import path (fresh interpreter)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REPO = Path(__file__).resolve().parents[2]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO)!r})\n{code}"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_import_leaves_asyncio_absent():
    proc = _run(
        "import sys\n"
        "import coordinator_core.archive_stamp\n"
        "print('asyncio' in sys.modules)\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"
