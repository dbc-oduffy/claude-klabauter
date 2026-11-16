"""Replay of b1abb0: the test landed at its run blob while deferred_emit_script.py lost its
_RECEIPT_PATH_JS block; the refire restore seam names the module as stranded, advisory, and the refire proceeds."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, ask_stage_plan, kept_work, resume_snapshot, stranded_sibling
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

MOD = "coordinator_core/ops/dispatch_emit/deferred_emit_script.py"
TST = "coordinator_core/ops/dispatch_emit/tests/test_deferred_emit_script_receipt.py"
RUN = "b1abb0"
BASE_MOD = b"x = 1\n"
RUN_MOD = b"x = 1\n_RECEIPT_PATH_JS = 'js'\n"
BASE_TST = b"def test_a():\n    pass\n"
RUN_TST = b"def test_a():\n    assert _RECEIPT_PATH_JS\n"


def _git(repo, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _world(repo, *, land_module: bool):
    """HEAD holds the test at its run blob; the module is at its run blob in HEAD only when
    `land_module`, else it stays the block-less base."""
    _git(repo, "init", "-q")
    for rel, data in ((MOD, BASE_MOD), (TST, BASE_TST)):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / TST).write_bytes(RUN_TST)
    if land_module:
        (repo / MOD).write_bytes(RUN_MOD)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "land")
    rec = repo / "state" / "warp" / RUN
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(
        json.dumps({"rows": [{"writes": [MOD, TST]}], "doc_writes": [], "review_declared_paths": []}),
        encoding="utf-8",
    )
    stranded_sibling.write(rec, {
        MOD: {"base": blob_sha(BASE_MOD), "run": blob_sha(RUN_MOD)},
        TST: {"base": blob_sha(BASE_TST), "run": blob_sha(RUN_TST)},
    })
    return rec


@pytest.fixture
def kept_row_holds_otherwise(monkeypatch):
    """The kept run's own fingerprints hold, so only the stranded check could change the verdict."""
    monkeypatch.setattr(kept_work, "read", lambda _d: {"chunks": {"R1": {"paths": []}}})
    monkeypatch.setattr(kept_work, "still_kept", lambda *a, **k: True)


@pytest.mark.parametrize("seam", [ask_stage._kept_row_holds, ask_stage_plan._kept_row_holds])
def test_refire_names_a_module_that_lost_its_block_and_still_proceeds(tmp_path, seam, kept_row_holds_otherwise):
    rec = _world(tmp_path, land_module=False)
    snap = resume_snapshot.Snapshot(frozenset(), frozenset(), kept=frozenset({"R1"}), kept_run=RUN)
    hits: list = []
    assert seam(tmp_path, snap, "R1", "report.md", hits) is True
    assert [(h["path"], h["reason"], h["run"]) for h in hits] == [(MOD, "lost", RUN)]
    assert not (rec / "tree-write-refusals.json").exists()


@pytest.mark.parametrize("seam", [ask_stage._kept_row_holds, ask_stage_plan._kept_row_holds])
def test_a_fully_landed_run_names_nothing(tmp_path, seam, kept_row_holds_otherwise):
    _world(tmp_path, land_module=True)
    snap = resume_snapshot.Snapshot(frozenset(), frozenset(), kept=frozenset({"R1"}), kept_run=RUN)
    hits: list = []
    assert seam(tmp_path, snap, "R1", "report.md", hits) is True
    assert hits == []


def test_a_record_with_no_landed_path_claims_nothing(tmp_path):
    rec = _world(tmp_path, land_module=False)
    stranded_sibling.write(rec, {MOD: {"base": blob_sha(BASE_MOD), "run": blob_sha(RUN_MOD)}})
    assert ask_stage_plan.stranded_after_restore(tmp_path, rec) == []


def test_a_record_dir_without_touched_blobs_claims_nothing(tmp_path):
    _world(tmp_path, land_module=False)
    empty = tmp_path / "state" / "warp" / "none"
    empty.mkdir(parents=True)
    assert ask_stage_plan.stranded_after_restore(tmp_path, empty) == []
