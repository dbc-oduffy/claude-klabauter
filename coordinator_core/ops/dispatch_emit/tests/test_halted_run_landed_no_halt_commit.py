"""dispatch.record_halted_run_landed records a hand-landed run that halted with no halt commit as landed against its real halt."""

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

RUN = "ask-20261124T094746-40ed59"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "commit", "-q", "-m", "hand-land")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _ran(root: Path, stage: str = "staged", **files: dict) -> Path:
    rd = record_dir(root, RUN)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": stage, "staged_at": "2026-11-24T09:49:09Z"}), encoding="utf-8")
    for name, doc in files.items():
        (rd / name).write_text(json.dumps(doc), encoding="utf-8")
    return rd


def _assert_recorded(out: dict, sha: str, kind: str) -> None:
    assert out["ok"] is True and out["written"] is True and out["sha"] == sha, out
    assert out["halted"] == kind, out
    assert kind in out["close"]["detail"], out
    assert "never ran" not in json.dumps(out) and "never halted" not in json.dumps(out), out


def test_co_tenant_halted_run_with_no_halt_commit_records_landed_against_its_halt(repo):
    root, sha = repo
    rd = _ran(root, **{"overlay-verdict.json": {"zero_run_cause": "co_tenant_dependency_on_halted_run", "verdict": "red"}})
    out = op._handler({"run_id": RUN, "sha": sha}, root)
    _assert_recorded(out, sha, "co_tenant_dependency_on_halted_run")
    marker = json.loads((rd / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == halted_run.STATUS_LANDED and marker["halted"] == "co_tenant_dependency_on_halted_run"
    assert marker["sha"] == sha


def test_run_with_no_halt_marker_and_no_cause_evidence_records_landed_as_unrecorded_end(repo):
    root, sha = repo
    rd = _ran(root)
    out = op._handler({"run_id": RUN, "sha": sha}, root)
    _assert_recorded(out, sha, halted_run.UNRECORDED_END_HALT)
    assert json.loads((rd / halted_run.HALTED_FILE).read_text(encoding="utf-8"))["status"] == halted_run.STATUS_LANDED


def test_recorded_run_replays_idempotently(repo):
    root, sha = repo
    _ran(root)
    op._handler({"run_id": RUN, "sha": sha}, root)
    again = op._handler({"run_id": RUN, "sha": sha}, root)
    assert again["ok"] is True and again["written"] is False, again


@pytest.mark.parametrize("stage", ["minted", None])
def test_run_that_never_left_minted_still_refuses_as_never_ran(repo, stage):
    root, sha = repo
    rd = _ran(root, stage=stage)
    out = op._handler({"run_id": RUN, "sha": sha}, root)
    assert out["ok"] is False and "never ran" in out["error"], out
    assert not (rd / halted_run.HALTED_FILE).exists()


def test_run_with_no_record_dir_still_refuses_as_never_ran(repo):
    root, sha = repo
    out = op._handler({"run_id": RUN, "sha": sha}, root)
    assert out["ok"] is False and "never ran" in out["error"], out
    assert not record_dir(root, RUN).exists()


def test_unverifiable_sha_on_a_ran_run_is_refused_not_recorded(repo):
    root, _ = repo
    _ran(root)
    out = op._handler({"run_id": RUN, "sha": "0" * 40}, root)
    assert out["ok"] is False, out
