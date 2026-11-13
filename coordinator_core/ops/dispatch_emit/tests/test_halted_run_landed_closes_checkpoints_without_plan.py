"""dispatch.record_halted_run_landed stamps Checkpoint-Run-Closed for the run's own bases, plan named or not."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_bases
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261112T000000-abc123"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _repo(tmp_path: Path, plan_rel):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "commit", "-q", "-m", "one")
    base = _git(tmp_path, "rev-parse", "HEAD")
    rd = record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(rd, halted="halted", sizing_rel=None, plan_rel=plan_rel, run_base_sha=base)
    _git(tmp_path, "add", "--", rd.relative_to(tmp_path).as_posix())
    _git(tmp_path, "commit", "-q", "-m", "halt record")
    return base, rd


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.mark.parametrize("plan_rel", [None, "docs/plans/does-not-exist.md"])
def test_records_commit_closes_the_run_base_without_a_real_plan(tmp_path, plan_rel):
    base, _ = _repo(tmp_path, plan_rel)
    out = op._handler({"run_id": RUN, "sha": base}, tmp_path)
    assert out["ok"] and out["records_commit"] == _git(tmp_path, "rev-parse", "HEAD"), out
    assert run_closed_bases(_git(tmp_path, "log", "-1", "--format=%B")) == {base}


def test_unverified_sha_stamps_no_closed_trailer(tmp_path):
    base, rd = _repo(tmp_path, None)
    halted_run.mark_landed(rd, base)
    out = op._commit_records(tmp_path, RUN, rd, base, False)
    assert "records_commit" in out
    assert run_closed_bases(_git(tmp_path, "log", "-1", "--format=%B")) == frozenset()


def test_refused_sha_closes_nothing(tmp_path):
    _repo(tmp_path, None)
    head = _git(tmp_path, "rev-parse", "HEAD")
    out = op._handler({"run_id": RUN, "sha": "0" * 40}, tmp_path)
    assert out["ok"] is False
    assert _git(tmp_path, "rev-parse", "HEAD") == head
