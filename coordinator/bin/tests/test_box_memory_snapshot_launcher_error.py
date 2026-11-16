"""The entry script the launcher runs prints a typed ERROR admit verdict when its helper module cannot load."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

SCRIPT = Path(__file__).resolve().parents[1] / "box-memory-snapshot.py"


def test_helper_load_failure_prints_typed_error_json(tmp_path):
    lone = tmp_path / "box-memory-snapshot.py"
    shutil.copy(SCRIPT, lone)  # no sibling lib/: the helper module load fails
    proc = subprocess.run(
        [sys.executable, "-I", str(lone), "--json", "--admit-peak", "12"],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 5, proc.stderr
    admit = json.loads(proc.stdout)["admit"]
    assert admit["verdict"] == "ERROR" and admit["error"]
    assert proc.stderr.strip()


def test_helper_load_failure_without_json_keeps_traceback(tmp_path):
    lone = tmp_path / "box-memory-snapshot.py"
    shutil.copy(SCRIPT, lone)
    proc = subprocess.run(
        [sys.executable, "-I", str(lone), "--admit-peak", "12"],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode not in (0, 4, 5)
    assert proc.stdout == ""


def _run(*args):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_usage_error_under_json_admit_prints_typed_error():
    proc = _run("--json", "--admit-peak", "notanumber")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["admit"]["verdict"] == "ERROR"
    assert "usage" in proc.stderr


def test_abbreviated_admit_flag_is_refused_with_typed_error():
    proc = _run("--json", "--admit", "12")
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["admit"]["verdict"] == "ERROR"


def test_usage_error_without_admit_flag_stays_argparse_exit_2():
    proc = _run("--json", "--bogus")
    assert proc.returncode == 2
    assert proc.stdout == ""


def test_exception_after_typed_json_does_not_print_a_second_object(monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("box_memory_snapshot_dup", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def late_failure(argv=None, **kwargs):
        mod._print_admit_error("already refused", 12.0, verdict="HOLD")
        raise RuntimeError("late")

    monkeypatch.setattr(mod, "_main", late_failure)
    assert mod.main(["--json", "--admit-peak", "12"]) == mod.EXIT_ERROR
    out = capsys.readouterr().out
    assert json.loads(out)["admit"]["verdict"] == "HOLD"
