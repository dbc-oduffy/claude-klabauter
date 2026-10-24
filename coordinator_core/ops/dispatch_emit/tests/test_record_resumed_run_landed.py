"""record_halted_run_landed on a solo ``resumed`` marker: accepted with no live successor, refused with one."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "warp-2026-10-20-solo-20261020T000000-abcdef"
SUCC = "warp-2026-10-20-solo-20261020T010000-123456"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)
    _git(tmp_path, "init", "-q")
    (tmp_path / "f").write_text("1", encoding="utf-8")
    _git(tmp_path, "add", "f")
    _git(tmp_path, "commit", "-q", "-m", "one")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _marker(root: Path, status: str = "resumed") -> Path:
    d = root / "state" / "warp" / RID
    d.mkdir(parents=True, exist_ok=True)
    p = d / "halted.json"
    p.write_text(json.dumps({"status": status, "halted": "x", "sizing_rel": None}), encoding="utf-8")
    return p


def _successor(root: Path, base: str, resumed_from: str = RID) -> None:
    d = root / base / SUCC
    d.mkdir(parents=True, exist_ok=True)
    (d / rl.RUN_FILE).write_text(json.dumps({rl.RESUMED_FROM_FIELD: resumed_from}), encoding="utf-8")


def test_resumed_with_no_successor_lands(repo):
    root, sha = repo
    p = _marker(root)
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == sha
    assert json.loads(p.read_text(encoding="utf-8"))["status"] == "landed"


@pytest.mark.parametrize("base", ["state/warp", "scratch/warp"])
def test_resumed_with_live_successor_refuses_naming_it(repo, monkeypatch, base):
    root, sha = repo
    p = _marker(root)
    before = p.read_bytes()
    _successor(root, base)
    monkeypatch.setattr(run_token, "is_live", lambda r, run: run == SUCC)
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and SUCC in out["error"]
    assert p.read_bytes() == before


def test_resumed_with_dead_or_unrelated_successor_lands(repo, monkeypatch):
    root, sha = repo
    _marker(root)
    _successor(root, "scratch/warp", resumed_from="warp-other")
    monkeypatch.setattr(run_token, "is_live", lambda r, run: True)
    assert op._handler({"run_id": RID, "sha": sha}, root)["ok"]


def test_resumed_with_non_live_successor_lands(repo, monkeypatch):
    root, sha = repo
    _marker(root)
    _successor(root, "state/warp")
    monkeypatch.setattr(run_token, "is_live", lambda r, run: False)
    assert op._handler({"run_id": RID, "sha": sha}, root)["ok"]


def test_running_status_still_refuses(repo):
    root, sha = repo
    p = _marker(root, "running")
    before = p.read_bytes()
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and "halted or resumed" in out["error"]
    assert p.read_bytes() == before
