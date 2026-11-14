"""dispatch.record_halted_run_landed records a staged run with no halt record and closes its run-open checkpoints."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import run_closed_bases
from coordinator_core.win_portability import no_console_creationflags

# percolate is outside the published restricted tree; a top-level import fails its import-closure gate.
_open_checkpoints = pytest.importorskip("coordinator_core.percolate.source_preflight")._open_checkpoints

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261114T000000-abc123"
PLAN = "docs/plans/p.md"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, stage):
    """A run whose wave checkpoint committed ``pkg/a.py`` and the plan, then a hand-land that touches only ``pkg/b.py``."""
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, {"one": "one"}, "one")
    rel = (record_dir(tmp_path, RUN) / "manifest.json").relative_to(tmp_path).as_posix()
    _commit(tmp_path, {rel: json.dumps({"head_sha": base})}, "manifest")
    _commit(
        tmp_path, {"pkg/a.py": "a = 1\n", PLAN: "plan\n"},
        f"checkpoint(wave 0): 1 rows — C1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Not-Landed: run-open\n"
        f"Checkpoint-Base: {base}\nCheckpoint-Withheld: pkg/b.py",
    )
    land = _commit(tmp_path, {"pkg/b.py": "b = 1\n"}, "hand-land")
    if stage:
        ask_contract.write_run_stage(tmp_path, RUN, stage)
    return base, land


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


def test_staged_run_with_no_halt_record_is_recorded_and_its_checkpoint_closed(tmp_path):
    # percolate is not in klabauter's restricted publish tree; the import-closure gate refuses a top-level import.
    _open_checkpoints = pytest.importorskip("coordinator_core.percolate.source_preflight")._open_checkpoints
    base, land = _repo(tmp_path, "staged")
    assert [plan for _, plan in _open_checkpoints(tmp_path, base)] == [PLAN]
    out = op._handler({"run_id": RUN, "sha": land}, tmp_path)
    assert out["ok"] is True and out["sha"] == land, out
    assert out["checkpoint_close_commit"] == _git(tmp_path, "rev-parse", "HEAD")
    assert run_closed_bases(_git(tmp_path, "log", "-1", "--format=%B")) == {base}
    assert _open_checkpoints(tmp_path, base) == []


def test_run_that_never_staged_still_refuses_a_landing_that_misses_the_checkpoint_paths(tmp_path):
    _, land = _repo(tmp_path, None)
    head = _git(tmp_path, "rev-parse", "HEAD")
    out = op._handler({"run_id": RUN, "sha": land}, tmp_path)
    assert out["ok"] is False and "pkg/a.py" in out["error"], out
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_staged_run_with_an_unverifiable_sha_closes_nothing(tmp_path):
    _repo(tmp_path, "staged")
    head = _git(tmp_path, "rev-parse", "HEAD")
    out = op._handler({"run_id": RUN, "sha": "0" * 40}, tmp_path)
    assert out["ok"] is False
    assert _git(tmp_path, "rev-parse", "HEAD") == head


def test_run_with_no_record_at_all_keeps_the_never_ran_refusal(tmp_path):
    _git(tmp_path, "init", "-q")
    only = _commit(tmp_path, {"one": "one"}, "one")
    out = op._handler({"run_id": RUN, "sha": only}, tmp_path)
    assert out["ok"] is False and "never ran" in out["error"], out
