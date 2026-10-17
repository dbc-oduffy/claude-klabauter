"""Pins that importing engine_provenance_counter defers the record call's closure, and that one
call writes exactly one six-field record."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_DEFERRED = ("coordinator_core.machine_profile", "coordinator_core.session.claimed_write")

_SCRIPT = r"""
import json, sys
from coordinator_core import engine_provenance_counter as e
before = [m for m in %r if m in sys.modules]
e.resolve_git_root_cheap = lambda cwd=None: sys.argv[1]
e._may_write = lambda root: True
e.record_engine_provenance("c", "dispatch", "match", "f.py", "/r")
print(json.dumps(before))
"""


@pytest.mark.spawns_process
def test_import_defers_closure_and_call_writes_one_record(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _SCRIPT % (list(_DEFERRED),), str(tmp_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(REPO), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
    lines = (tmp_path / "state" / "engine-provenance-counts.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert set(json.loads(lines[0])) == {
        "caller", "axis", "verdict", "imported_file", "engine_root", "at",
    }
