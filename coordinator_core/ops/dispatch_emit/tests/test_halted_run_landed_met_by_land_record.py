"""A met_by credit on a run with no halt record is placed by its committed land-legs.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261120T000000-68c477"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo: Path, rel: str, text: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", rel)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


def _setup(tmp_path: Path, outcome):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "one", "one")
    land = _commit(tmp_path, "pkg/a.py", "a = 1\n")
    rd = record_dir(tmp_path, RUN)
    rd.mkdir(parents=True, exist_ok=True)
    if outcome:
        (rd / "land-legs.json").write_text(json.dumps({"outcome": outcome}), encoding="utf-8")
    return land, rd


@pytest.mark.parametrize("outcome", ["landed", "landed_no_code", "partial"])
def test_committed_land_record_credits_rows(tmp_path, outcome):
    land, rd = _setup(tmp_path, outcome)
    out = op._handler({"run_id": RUN, "sha": land, "met_by": {"R1": land, "R2": land}}, tmp_path)
    assert out["ok"] is True, out
    assert out["met_by"] == {"R1": land, "R2": land}
    assert halted_run._read(rd)["met_by"] == {"R1": land, "R2": land}


def test_no_terminal_record_still_refuses(tmp_path):
    land, rd = _setup(tmp_path, None)
    out = op._handler({"run_id": RUN, "sha": land, "met_by": {"R1": land}}, tmp_path)
    assert out["ok"] is False, out
    assert halted_run._read(rd) is None


def test_halt_land_outcome_is_not_a_commit(tmp_path):
    land, _ = _setup(tmp_path, "land-deadline")
    out = op._handler({"run_id": RUN, "sha": land, "met_by": {"R1": land}}, tmp_path)
    assert out["ok"] is False, out
