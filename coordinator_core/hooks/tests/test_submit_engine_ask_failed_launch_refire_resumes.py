"""A refire whose emitted script never launched leaves the prior run resumable: a plain refire after a failed
launch resumes it, while a successor that started keeps it superseded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask import _Emit, _hso, _payload, _repo
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    PLAN_STARTED_FIELD, RECORD_DIR_ROOT, stamp_plan_phase_at, write_run_stage_at,
)

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-24-failed-launch.yaml"
RUN = "warp-2026-11-24-failed-launch-20261124T010101-abc123"
SUCCESSOR = "ask-20261013T000000-abcdef"  # the id `_Emit` mints
ENGINE = "a" * 40


def _put(path: Path, doc) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _halted_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = _repo(tmp_path)
    (root / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-failed-launch-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-24", "mode": "pm"}},
    }
    (root / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rd = root / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    _put(rd / "halted.json", {"status": "halted", "halted": "commit", "sizing_rel": SIZING_REL, "engine_sha": ENGINE})
    _put(rd / "run-stage.json", {"stage": "commit"})
    return root, rd


def _marker(rd: Path) -> dict:
    return json.loads((rd / "halted.json").read_text(encoding="utf-8"))


def _fresh_refire(root: Path, monkeypatch: pytest.MonkeyPatch) -> _Emit:
    monkeypatch.setattr(hrr, "engine_sha", lambda: ENGINE)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: True)
    emit = _Emit()
    out = mod.evaluate(_payload(root, {"sizing": SIZING_REL, "fresh": True}), emit=emit)
    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    return emit


def _mint_successor(root: Path, *, started: bool) -> Path:
    srd = root / RECORD_DIR_ROOT / SUCCESSOR
    srd.mkdir(parents=True, exist_ok=True)
    write_run_stage_at(srd, "minted")
    if started:
        assert stamp_plan_phase_at(srd) is True
    return srd


def test_a_plain_refire_after_a_launch_that_never_started_resumes_the_prior_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, rd = _halted_repo(tmp_path)
    _fresh_refire(root, monkeypatch)
    srd = _mint_successor(root, started=False)
    marker = _marker(rd)
    assert marker["status"] == "superseded"
    assert marker["superseded_evidence"]["successor"] == SUCCESSOR
    assert marker["superseded_evidence"]["prior_status"] == "halted"

    emit = _Emit()
    out = mod.evaluate(_payload(root, {"sizing": SIZING_REL}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert emit.calls == []
    assert RUN in json.dumps(out)
    assert SUCCESSOR not in json.dumps(out)
    after = _marker(rd)
    assert after["status"] in ("halted", "resumed")
    assert "superseded_evidence" not in after
    assert after["voided_supersede"]["kind"] == halted_run.SUPERSEDED_BY_REFIRE
    assert _marker(srd)["status"] == "superseded"


def test_a_refire_whose_successor_started_keeps_the_prior_run_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, rd = _halted_repo(tmp_path)
    _fresh_refire(root, monkeypatch)
    srd = _mint_successor(root, started=True)
    assert PLAN_STARTED_FIELD in json.loads((srd / "run-stage.json").read_text(encoding="utf-8"))

    assert halted_run.void_unstarted_supersede(root, SIZING_REL) is None

    assert _marker(rd)["status"] == "superseded"
    assert halted_run.halted_run_to_resume(root, SIZING_REL) is None
    assert not (srd / "halted.json").exists()


def test_a_successor_past_minted_keeps_the_prior_run_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, rd = _halted_repo(tmp_path)
    _fresh_refire(root, monkeypatch)
    srd = _mint_successor(root, started=False)
    write_run_stage_at(srd, "staged")

    assert halted_run.void_unstarted_supersede(root, SIZING_REL) is None
    assert _marker(rd)["status"] == "superseded"


def test_only_the_newest_capped_superseded_markers_are_scanned_for_a_void(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, rd = _halted_repo(tmp_path)
    _fresh_refire(root, monkeypatch)
    _mint_successor(root, started=False)
    assert halted_run._VOID_SCAN_CAP == 3
    for i in range(1, halted_run._VOID_SCAN_CAP + 1):
        newer = f"warp-2026-11-24-failed-launch-20261124T02020{i}-abc12{i}"
        newer_rd = root / RECORD_DIR_ROOT / newer
        newer_rd.mkdir(parents=True)
        succ = f"ask-20261124T03030{i}-aaaaa{i}"
        srd = root / RECORD_DIR_ROOT / succ
        srd.mkdir(parents=True)
        write_run_stage_at(srd, "minted")
        assert stamp_plan_phase_at(srd) is True
        halted_run._write(newer_rd, {
            "status": "superseded", "halted": newer, "sizing_rel": SIZING_REL,
            "superseded_evidence": {
                "kind": halted_run.SUPERSEDED_BY_REFIRE, "successor": succ, "prior_status": "halted",
            },
        })

    assert halted_run.void_unstarted_supersede(root, SIZING_REL) is None
    assert _marker(rd)["status"] == "superseded"


def test_a_supersedes_whose_successor_never_started_reads_as_not_superseded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    root, _rd = _halted_repo(tmp_path)
    held = "ask-20261013T000001-aaaaaa"
    emit = _Emit()
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: True)
    out = mod.evaluate(_payload(root, {"ask": "do the thing", "supersedes": held}), emit=emit)
    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    assert (root / contract.superseded_rel(held)).is_file()

    srd = _mint_successor(root, started=False)
    assert contract.read_superseded_by(root, held) is None

    stamp_plan_phase_at(srd)
    assert contract.read_superseded_by(root, held) == SUCCESSOR
