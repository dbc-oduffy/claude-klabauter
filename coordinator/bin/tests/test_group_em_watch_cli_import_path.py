"""group-em-watch entry points resolve cc_invoke without the script dir on sys.path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(script: str, tmp_path: Path):
    # -P keeps the script dir off sys.path[0]; cwd is foreign.
    return subprocess.run(
        [sys.executable, "-P", str(BIN / script), "--repo-root", str(tmp_path)]
        if script == "group-em-watch-cli.py"
        else [sys.executable, "-P", str(BIN / script), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=NO_CONSOLE,
    )


@pytest.mark.skipif(sys.version_info < (3, 11), reason="-P needs 3.11+")
def test_cli_prints_verdict_without_script_dir_on_path(tmp_path):
    proc = _run("group-em-watch-cli.py", tmp_path)
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.stdout.startswith("GROUP EM WATCH:"), (proc.stdout, proc.stderr)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="-P needs 3.11+")
def test_group_em_watch_py_resolves_cc_invoke_without_script_dir(tmp_path):
    proc = _run("group-em-watch.py", tmp_path)
    assert "No module named 'lib'" not in proc.stderr, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
