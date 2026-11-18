"""A run closed as superseded-by-refire earns the fresh mint no kept-chunk credit and keeps its holds; the
deferred emit's stamp check closes a run only when the engine stamps differ."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import halted_hold_release, kept_work, op, own_records_resume, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import SUPERSEDED_BY_REFIRE

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-18-credit-fixture.yaml"
RUN = "warp-2026-11-18-credit-fixture-20261118T010101-abc123"
KEPT = "a/two.py"
OLD, NEW = "a" * 40, "b" * 40


def _run(root: Path, marker: dict) -> Path:
    (root / ".git").mkdir(exist_ok=True)
    (root / KEPT).parent.mkdir(parents=True, exist_ok=True)
    (root / KEPT).write_text("# two\n", encoding="utf-8")
    rd = root / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    (rd / "halted.json").write_text(json.dumps({"halted": "commit", "sizing_rel": SIZING_REL, **marker}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "commit"}), encoding="utf-8")
    (rd / "manifest.json").write_text(json.dumps({"rows": [{"id": "C2", "writes": [KEPT]}]}), encoding="utf-8")
    (rd / "kept.json").write_text(json.dumps({
        "chunks": {"C2": {"paths": {KEPT: kept_work.fingerprint(root, KEPT)}}}, "plan_gate": None, "post_checkpoint": {},
    }), encoding="utf-8")
    return rd


def _superseded(kind: str | None) -> dict:
    evidence = {"kind": kind} if kind else {}
    return {"status": "superseded", "superseded_evidence": evidence}


@pytest.fixture
def credited(monkeypatch):
    """The run's own records show every row done and reviewed."""
    monkeypatch.setattr(own_records_resume, "landing_resume_from_own_records", lambda root, name: object())


def test_a_resumed_run_credits_its_kept_row(tmp_path, credited):
    _run(tmp_path, {"status": "resumed"})
    assert sizing_fire.own_records_run(tmp_path, SIZING_REL) == RUN
    assert sizing_fire._kept_rows(tmp_path, SIZING_REL, ["C1", "C2"]) == (("C2",), RUN)


def test_a_superseded_by_refire_run_credits_nothing(tmp_path, credited):
    _run(tmp_path, _superseded(SUPERSEDED_BY_REFIRE))
    assert sizing_fire.own_records_run(tmp_path, SIZING_REL) is None
    assert sizing_fire._kept_rows(tmp_path, SIZING_REL, ["C1", "C2"]) == ((), None)


def test_a_fresh_mint_beside_the_closed_run_credits_nothing(tmp_path, credited):
    _run(tmp_path, _superseded(SUPERSEDED_BY_REFIRE))
    fresh = tmp_path / RECORD_DIR_ROOT / "warp-2026-11-18-credit-fixture-20261118T020202-def456"
    fresh.mkdir()
    (fresh / "run-stage.json").write_text(json.dumps({"stage": "composed"}), encoding="utf-8")
    assert sizing_fire._kept_rows(tmp_path, SIZING_REL, ["C2"]) == ((), None)


def test_the_closed_runs_withheld_paths_stay_held(tmp_path):
    _run(tmp_path, _superseded(SUPERSEDED_BY_REFIRE))
    assert halted_hold_release.supersession_reason(tmp_path, RUN) is None


def test_any_other_superseded_run_still_releases_its_holds(tmp_path):
    _run(tmp_path, _superseded("withheld-paths-superseded"))
    assert halted_hold_release.supersession_reason(tmp_path, RUN) == halted_hold_release.RELEASE_MARKER_SUPERSEDED


@pytest.mark.parametrize("stamp, running, closes", [(OLD, NEW, True), (OLD, OLD, False), (None, NEW, False), (OLD, None, False)])
def test_the_deferred_emit_closes_only_on_a_differing_stamp(tmp_path, monkeypatch, stamp, running, closes):
    marker = {"status": "halted", **({"engine_sha": stamp} if stamp else {})}
    rd = _run(tmp_path, marker)
    monkeypatch.setattr(hrr, "engine_sha", lambda: running)
    assert op._closed_for_fresh_refire(tmp_path, RUN) is closes
    written = json.loads((rd / "halted.json").read_text(encoding="utf-8"))
    assert written["status"] == ("superseded" if closes else "halted")
    if closes:
        assert written["superseded_evidence"]["kind"] == SUPERSEDED_BY_REFIRE


def test_the_deferred_emit_never_closes_a_landed_run(tmp_path, monkeypatch):
    _run(tmp_path, {"status": "landed", "engine_sha": OLD})
    monkeypatch.setattr(hrr, "engine_sha", lambda: NEW)
    assert op._closed_for_fresh_refire(tmp_path, RUN) is False
