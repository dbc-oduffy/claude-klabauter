"""Exit criterion end to end through the default refire call (`{sizing}` only): a halted or resumed run that ran
under a different engine stamp closes as superseded-by-refire, the refire mints a new run id, and no kept chunk
of the closed run is credited to it; its notes stay reachable and its withheld paths stay held."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask import _Emit, _hso, _payload, _repo
from coordinator_core.ops.dispatch_emit import halted_hold_release, kept_work, own_records_resume, sizing_fire
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import SUPERSEDED_BY_REFIRE, notes_predecessor

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-18-e2e-fresh.yaml"
RUN = "warp-2026-11-18-e2e-fresh-20261118T010101-abc123"
FRESH_RUN = "ask-20261013T000000-abcdef"  # the id `_Emit` mints
KEPT = "a/one.py"
WITHHELD = "a/held.py"
OLD, NEW = "a" * 40, "b" * 40
NOTE = "keep the exit criterion literal"


def _put(path: Path, doc) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _run_repo(tmp_path: Path, *, status: str) -> tuple[Path, Path]:
    root = _repo(tmp_path)
    (root / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-e2e-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-18", "mode": "pm"}},
    }
    (root / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    for rel in (KEPT, WITHHELD):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"# {rel}\n", encoding="utf-8")
    rd = root / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    _put(rd / "manifest.json", {"rows": [{"id": "C1", "writes": [KEPT]}]})
    _put(rd / "kept.json", {
        "chunks": {"C1": {"paths": {KEPT: kept_work.fingerprint(root, KEPT)}}}, "plan_gate": None, "post_checkpoint": {},
    })
    _put(rd / "halted.json", {
        "status": status, "halted": "commit", "sizing_rel": SIZING_REL, "engine_sha": OLD,
        "run_artifacts": {"withheld": {WITHHELD: "unreviewed"}},
    })
    _put(rd / "run-stage.json", {"stage": "commit"})
    (rd / "notes.jsonl").write_text(json.dumps({"note": NOTE, "at": "2026-11-18T00:00:00Z"}) + "\n", encoding="utf-8")
    return root, rd


@pytest.mark.parametrize("status", ["halted", "resumed"])
def test_default_refire_after_an_engine_swap_supersedes_the_run_and_mints_fresh_with_no_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    root, rd = _run_repo(tmp_path, status=status)
    monkeypatch.setattr(hrr, "engine_sha", lambda: NEW)
    # A `resumed` run is credited through its own records until the refire closes it.
    monkeypatch.setattr(own_records_resume, "landing_resume_from_own_records", lambda r, name: object())
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: pytest.fail("a superseded run must not recompose"))
    if status == "resumed":
        assert sizing_fire._kept_rows(root, SIZING_REL, ["C1"]) == (("C1",), RUN)
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING_REL}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    marker = json.loads((rd / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "superseded"
    assert marker["superseded_evidence"]["kind"] == SUPERSEDED_BY_REFIRE
    assert marker["superseded_evidence"]["prior_engine"] == OLD and marker["superseded_evidence"]["engine"] == NEW
    assert len(emit.calls) == 1
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params and params.get("resume_run_id") is None
    minted = FRESH_RUN
    assert minted != RUN
    assert sizing_fire._kept_rows(root, SIZING_REL, ["C1"]) == ((), None)
    assert notes_predecessor(root, SIZING_REL, exclude=minted) == RUN
    assert halted_hold_release.supersession_reason(root, RUN) is None
    assert marker["run_artifacts"]["withheld"] == {WITHHELD: "unreviewed"}


def test_default_refire_under_the_same_stamp_resumes_the_run_and_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, rd = _run_repo(tmp_path, status="halted")
    monkeypatch.setattr(hrr, "engine_sha", lambda: OLD)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: True)
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING_REL}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert emit.calls == []
    assert json.loads((rd / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"


def test_fresh_refire_of_a_cancelled_run_reaches_the_emit_with_fresh_and_the_emit_mints_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """holo-5e 5d606f, 2026-11-23: a workflow.cancel'd run refused `fresh: true` three times with its own remedy."""
    from coordinator_core.ops.dispatch_emit import op

    root, rd = _run_repo(tmp_path, status="halted")
    _put(rd / "halted.json", {**json.loads((rd / "halted.json").read_text(encoding="utf-8")), "halted": "cancelled"})
    monkeypatch.setattr(hrr, "engine_sha", lambda: OLD)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: pytest.fail("a terminal run must not recompose"))
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING_REL, "fresh": True}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    params, _ = emit.calls[0]
    assert params.get("fresh") is True and params.get("resume_run_id") is None
    assert op._terminal_under_fresh(root, RUN, params) is True
    assert op._terminal_under_fresh(root, RUN, {}) is False
