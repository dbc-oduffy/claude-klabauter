"""run_close.sibling_release against a real git repo: the landed-record reconcile spawns git."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.tests.test_run_close_sibling_release import (
    SHA,
    _run,
    _seed,
    _status,
)
from coordinator_core.warp_fire import fire_command
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _landed_repo(root: Path, landed_record_for=()):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "base.txt").write_text("x\n", encoding="utf-8")
    for rid in landed_record_for:
        rec = root / "state" / "warp" / rid / "usage.json"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({"as_of": "terminal-commit"}), encoding="utf-8")
    _git(root, "add", "-A", "-f")
    _git(root, "commit", "-q", "-m", "terminal")


def _diamond(root):
    runs = [_run("A"), _run("B"), _run("C", ["A", "B"], c.STATUS_AFTER)]
    _seed(root, runs, bound=["A", "B"])


def test_running_but_landed_sibling_counts_as_landed_for_release(tmp_path):
    _diamond(tmp_path)
    _landed_repo(tmp_path, landed_record_for=["A"])
    out = run_close.sibling_release(tmp_path, "B", SHA)
    assert out["released"] == [fire_command("state/sizings/C.yaml")]
    assert _status(tmp_path, "A")["status"] == "landed"
    assert _status(tmp_path, "A")["reconciled_from"] == "running"


def test_running_sibling_without_record_at_head_does_not_release(tmp_path):
    _diamond(tmp_path)
    _landed_repo(tmp_path)
    out = run_close.sibling_release(tmp_path, "B", SHA)
    assert out["released"] == []
    assert _status(tmp_path, "A") == {"status": "running"}
