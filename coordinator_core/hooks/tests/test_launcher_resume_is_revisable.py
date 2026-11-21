"""A run the launcher resume starts takes `workflow.revise`: the resume route mints its run-parent record,
and the revision lands in the held inbox the resumed run's ledger composer reads at stage start."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID, _hso, _payload, _repo, seams,  # noqa: F401 (seams is a fixture)
)
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import parent_record_rel

pytestmark = pytest.mark.complete

VERBATIM = "measure, do not bump the RSS bound"


def _resume(root: Path, emit) -> None:
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert HALTED_ID in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")


def test_launcher_resumed_run_accepts_a_revision_and_queues_it_for_its_stages(tmp_path, seams):
    _, _, emit = seams
    root = _repo(tmp_path, record=False)
    _resume(root, emit)

    ledger, dir_, added = rl.revise(root, HALTED_ID, verbatim=VERBATIM)

    assert dir_ == root / RECORD_DIR_ROOT / HALTED_ID
    assert added == "Q1" and [i.text for i in ledger.items] == [VERBATIM]
    queued = [json.loads(line) for line in (dir_ / rl.INBOX_FILE).read_text(encoding="utf-8").splitlines()]
    assert [e["text"] for e in queued] == [VERBATIM]
    assert rl._inbox(dir_)[0]["text"] == VERBATIM


def test_stored_script_resume_also_mints_the_record(tmp_path, seams):
    _, _, emit = seams
    root = _repo(tmp_path)
    mod.evaluate(_payload(root), emit=emit)
    rl.revise(root, HALTED_ID, verbatim=VERBATIM)


def test_a_run_nobody_resumed_still_refuses_unknown_run(tmp_path, seams):
    _, _, emit = seams
    root = _repo(tmp_path, record=False)
    _resume(root, emit)
    with pytest.raises(rl.LedgerRefused, match="unknown run"):
        rl.revise(root, "warp-2026-10-29-resume-fixture-20261029T010101-zzzzzz", verbatim=VERBATIM)


def test_an_unwritable_parent_record_never_blocks_the_resume(tmp_path, seams, monkeypatch):
    _, _, emit = seams

    def boom(*a, **k):
        raise OSError("read-only")

    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.ask_fanout_contract.mint_run_parent", boom)
    root = _repo(tmp_path, record=False)
    _resume(root, emit)
    assert not (root / parent_record_rel(f"run-parent-{HALTED_ID}")).exists()
