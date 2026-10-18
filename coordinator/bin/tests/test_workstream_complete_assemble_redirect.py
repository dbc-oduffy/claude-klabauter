"""Pins that workstream-complete-assemble delivers its report on stdout when
stdout is redirected to a file, on both the served-result path and the real CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BIN_DIR = REPO_ROOT / "coordinator" / "bin"
sys.path.insert(0, str(BIN_DIR / "lib"))

import entry_point_shim  # noqa: E402


@pytest.fixture(autouse=True)
def _scrub_dispatching_session_identity(monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def test_native_route_writes_served_stdout_to_a_redirected_file(tmp_path, monkeypatch):
    report = json.dumps({"directives": [], "note": "non-ascii — dash"})
    served = {"stdout": report, "stderr": "wsc-apply: x\n", "exit_code": 4}

    class _Invoke:
        @staticmethod
        def route(op, params, repo_root, legacy_fn):
            return served

    monkeypatch.setitem(sys.modules, "cc_invoke", _Invoke)
    monkeypatch.setattr(entry_point_shim, "_merge_assemble_checked_repo_root", lambda: tmp_path)
    monkeypatch.delenv("COORDINATOR_EXECUTION_ROUTE", raising=False)
    monkeypatch.delenv("COORDINATOR_SERVED_ENTRYPOINT", raising=False)

    out_path = tmp_path / "out.txt"
    err_path = tmp_path / "err.txt"
    entry = entry_point_shim._native_route_entry(
        "workstream-complete-assemble", "coordinator_core.workstream_complete"
    )
    with open(out_path, "w", encoding="cp1252", errors="strict") as out, open(
        err_path, "w", encoding="utf-8"
    ) as err:
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        code = entry(["apply", "--decisions", "{}"])

    assert code == 4
    assert json.loads(out_path.read_text(encoding="cp1252"))["directives"] == []


@pytest.mark.spawns_process
def test_brief_stdout_redirected_to_file_matches_piped_report(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "tier-test-unanchored-session")
    script = BIN_DIR / "workstream-complete-assemble.py"
    argv = [sys.executable, str(script), "brief", "--decisions", "{}"]
    out_path = tmp_path / "out.txt"
    with open(out_path, "wb") as out:
        proc = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            stdout=out,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    redirected = json.loads(out_path.read_text(encoding="utf-8"))
    assert {"artifact", "directives", "next_move"} <= set(redirected)
