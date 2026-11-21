"""A dispatch.emit whose reply is lost (-32004) and re-sent yields exactly one run; the client names the re-send."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import op as emit_op
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_seed import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ASK = "add the lost-reply thing"


@pytest.fixture
def sessions(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("sessions")
    managed, legacy = base / "managed", base / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base / "claude"))
    return managed, base


def _run_dirs(root):
    return sorted(p.name for p in (root / RECORD_DIR_ROOT).iterdir() if p.is_dir())


_CHILD = """
import sys, time
from pathlib import Path
from coordinator_core.ops.dispatch_emit import op

_real = op._emit_script_file


def _write_then_hang(*args, **kwargs):
    out = _real(*args, **kwargs)
    print("WRITTEN", flush=True)
    time.sleep(600)
    return out


op._emit_script_file = _write_then_hang
op._dispatch_emit({"ask": sys.argv[2]}, Path(sys.argv[1]))
"""


def test_an_emit_killed_after_the_script_is_durable_and_resent_yields_one_run(repo, sessions, tmp_path):
    root = Path(__file__).resolve().parents[4]
    child = tmp_path / "emit_child.py"
    child.write_text(_CHILD, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen(
        [sys.executable, str(child), str(repo), ASK], cwd=root, env=env, stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    watchdog = threading.Timer(120, proc.kill)
    watchdog.start()
    try:
        assert proc.stdout.readline().strip() == b"WRITTEN"
        proc.kill()
        proc.wait()
    finally:
        watchdog.cancel()
        proc.kill()
    minted = _run_dirs(repo)
    assert len(minted) == 1

    resent = emit_op._dispatch_emit({"ask": ASK}, repo)

    assert resent["run_id"] == minted[0] and resent["deduped"] is True
    assert Path(resent["path"]).is_file() and resent["sha256"]
    assert resent["fire_args"]["runToken"]
    assert _run_dirs(repo) == minted
    assert [r["run_id"] for r in run_token.held_records(repo)] == minted
