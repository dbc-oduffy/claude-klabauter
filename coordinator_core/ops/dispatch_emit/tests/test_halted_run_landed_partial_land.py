"""dispatch.record_halted_run_landed closes a marker-less run whose land committed (``land-legs.json`` outcome landed, e.g. a partial land) and still refuses one that never committed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import land_leg_record
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-18-partial-land-11834c-20261118T000000-aaaaaa"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, rel: str, text: str, message: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> str:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "one", "one", "one")
    return _commit(tmp_path, "pkg/a.py", "a = 1\n", "the partial land commit")


def _land_record(tmp_path: Path, outcome: str) -> None:
    rd = record_dir(tmp_path, RUN)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / land_leg_record.FILE_NAME).write_text(json.dumps({"outcome": outcome, "legs": []}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


def test_a_committed_partial_land_is_recorded_landed_not_never_ran(tmp_path):
    sha = _repo(tmp_path)
    _land_record(tmp_path, land_leg_record.OUTCOME_LANDED)
    out = op._handler({"run_id": RUN, "sha": sha}, tmp_path)
    assert out["ok"] is True and out["sha"] == sha and out["written"] is False, out
    assert out["close"]["status"] == "not-applicable", out


def test_a_marker_less_run_with_no_land_record_keeps_the_never_ran_refusal(tmp_path):
    sha = _repo(tmp_path)
    out = op._handler({"run_id": RUN, "sha": sha}, tmp_path)
    assert out["ok"] is False and "never ran" in out["error"], out


def test_a_land_deadline_record_is_not_a_committed_land(tmp_path):
    sha = _repo(tmp_path)
    _land_record(tmp_path, land_leg_record.OUTCOME_HALT)
    out = op._handler({"run_id": RUN, "sha": sha}, tmp_path)
    assert out["ok"] is False and "never ran" in out["error"], out


def test_a_committed_land_still_refuses_a_sha_that_is_not_reachable(tmp_path):
    _repo(tmp_path)
    _land_record(tmp_path, land_leg_record.OUTCOME_LANDED)
    out = op._handler({"run_id": RUN, "sha": "0" * 40}, tmp_path)
    assert out["ok"] is False, out
