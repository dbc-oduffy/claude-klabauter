"""Subprocess tests of the real `trim-file-cache.py` entry script: the non-elevated and non-Windows
refusals. No test performs a trim; the Windows case skips when the test process is elevated."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

TOOLS_DIR = Path(__file__).resolve().parents[1]
SCRIPT = TOOLS_DIR / "trim-file-cache.py"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _process_is_elevated() -> bool:
    """The tool's own token check, so the skip and the refusal agree."""
    sys.path.insert(0, str(TOOLS_DIR / "lib"))
    try:
        from file_cache_win32 import Win32FileCacheHost

        return Win32FileCacheHost().is_elevated()
    finally:
        sys.path.remove(str(TOOLS_DIR / "lib"))


@pytest.mark.skipif(sys.platform != "win32", reason="elevation refusal is the Windows path")
@pytest.mark.parametrize("args", [(), ("--apply",)], ids=["dry-run", "apply"])
def test_non_elevated_refuses_with_exit_3(tmp_path, args):
    if _process_is_elevated():
        pytest.skip("test process is elevated; refusing to risk a real run")
    result = _run(tmp_path, *args)
    assert result.returncode == 3, result.stderr
    assert "elevated" in result.stderr.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows refusal")
@pytest.mark.parametrize("args", [(), ("--apply",)], ids=["dry-run", "apply"])
def test_non_windows_refuses_with_exit_6(tmp_path, args):
    result = _run(tmp_path, *args)
    assert result.returncode == 6, result.stderr
