"""Behavior pins for the unimatrix grok peer: registry discovery and the cwd-pinned headless resume."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subprocess import CompletedProcess

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_peer_grok", BIN / "unimatrix-peer-grok.py")
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)

SID = "01a1c000-0000-7000-8000-00000000c0c0"


def registry(tmp_path, rows):
    (tmp_path / "active_sessions.json").write_text(json.dumps(rows), encoding="utf-8")
    return tmp_path


def test_default_liveness_uses_real_pid_check(tmp_path: Path) -> None:
    rows = [{"session_id": SID, "pid": os.getpid(), "cwd": str(tmp_path)}]
    assert [p["target"] for p in pg.discover_peers(registry(tmp_path, rows))] == [SID]
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    rows = [{"session_id": SID, "pid": proc.pid, "cwd": str(tmp_path)}]
    assert pg.discover_peers(registry(tmp_path, rows)) == []


def test_own_session_real_walk_from_a_child_process_finds_the_registered_ancestor(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": os.getpid(), "cwd": str(tmp_path)}])
    code = ("import importlib.util,sys;"
            f"s=importlib.util.spec_from_file_location('g',r'{BIN / 'unimatrix-peer-grok.py'}');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"p=m.own_session(r'{home}');print(p['target'] if p else '')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == SID


def test_own_session_real_walk_from_a_child_process_finds_the_registered_ancestor(tmp_path: Path) -> None:
    home = registry(tmp_path, [{"session_id": SID, "pid": os.getpid(), "cwd": str(tmp_path)}])
    code = ("import importlib.util,sys;"
            f"s=importlib.util.spec_from_file_location('g',r'{BIN / 'unimatrix-peer-grok.py'}');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"p=m.own_session(r'{home}');print(p['target'] if p else '')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == SID
