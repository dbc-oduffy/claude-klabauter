"""A refire that retires the halted run and mints a fresh one carries the halted run's revise notes and
queued revisions into the new run and names them, with their source run, in the reply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (
    HALTED_ID,
    RECORD_DIR_ROOT,
    _hso,
    _payload,
    _repo,
    seams,  # noqa: F401  (fixture)
)
from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

FRESH = "warp-2026-10-29-resume-fixture-20261029T020202-fff000"
NOTE = "also add the targeted_test_launcher key"
QUEUED = "keep the old flag"


def _post(root: Path, run_id: str) -> None:
    d = root / RECORD_DIR_ROOT / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / rl.NOTES_FILE).write_text(json.dumps({"note": NOTE, "at": "2026-10-29T09:00:00Z"}) + "\n", encoding="utf-8")
    (d / rl.INBOX_FILE).write_text(json.dumps({"text": QUEUED, "at": "2026-10-29T09:01:00Z"}) + "\n", encoding="utf-8")


def _fresh_emit(params, root):
    rl.start(root, FRESH)
    return {"path": str(root / "emitted.js"), "run_id": FRESH, "fire_args": {}}


def _assert_carried(root: Path, env: dict) -> None:
    assert [e["note"] for e in rl.carried_notes(root, FRESH)] == [NOTE]
    assert [e["text"] for e in rl._inbox(root / RECORD_DIR_ROOT / FRESH)] == [QUEUED]
    hso = _hso(env)
    reason = hso.get("additionalContext", "") + hso.get("permissionDecisionReason", "")
    assert f"Carried from the retired halted run: {HALTED_ID} note: {NOTE}" in reason
    assert f"{HALTED_ID} queued: {QUEUED}" in reason


def test_a_spent_token_retire_carries_notes_and_names_them(tmp_path, seams, monkeypatch):  # noqa: F811
    spawned, emitted, _emit = seams
    root = _repo(tmp_path, record=False)
    _post(root, HALTED_ID)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.ask_gate.token_retired", lambda r, i: True)
    env = mod.evaluate(_payload(root), emit=_fresh_emit)
    assert _hso(env)["permissionDecision"] == "allow"
    _assert_carried(root, env)
    assert spawned == []


def test_the_recomposed_gate_retire_carries_notes_and_names_them(tmp_path, seams, monkeypatch):  # noqa: F811
    spawned, emitted, _emit = seams
    root = _repo(tmp_path, record=False)
    _post(root, HALTED_ID)
    monkeypatch.setattr(mod, "_spent_after_ensure", lambda r, i: True)
    monkeypatch.setattr(mod, "_ensure_launcher_run_token", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_default_launch", lambda root, parent_id, children, session: root / "launcher.js")
    payload = _payload(root)
    payload["tool_input"]["args"]["writes"] = ["a.py"]
    env = mod.evaluate(payload, emit=_fresh_emit)
    assert _hso(env)["permissionDecision"] == "allow"
    _assert_carried(root, env)


def test_a_retire_with_nothing_posted_says_nothing(tmp_path, seams, monkeypatch):  # noqa: F811
    root = _repo(tmp_path, record=False)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.ask_gate.token_retired", lambda r, i: True)
    env = mod.evaluate(_payload(root), emit=_fresh_emit)
    assert "Carried from" not in _hso(env).get("permissionDecisionReason", "")


def test_a_refire_that_retires_nothing_carries_nothing(tmp_path, seams, monkeypatch):  # noqa: F811
    root = _repo(tmp_path, record=False)
    _post(root, HALTED_ID)
    rl.start(root, FRESH)
    assert mod._carry_retired_revisions(root, [], FRESH) == ""
    assert rl.carry_notes(root, [FRESH], FRESH) == []
    assert rl._inbox(root / RECORD_DIR_ROOT / FRESH) == []


def test_carry_is_idempotent(tmp_path):
    root = _repo(tmp_path, record=False)
    _post(root, HALTED_ID)
    rl.start(root, FRESH)
    first = mod._carry_retired_revisions(root, [HALTED_ID], FRESH)
    assert NOTE in first and QUEUED in first
    assert mod._carry_retired_revisions(root, [HALTED_ID], FRESH) == ""
    assert [e["note"] for e in rl.carried_notes(root, FRESH)] == [NOTE]


SIZING = "state/sizings/2026-10-29-two-halts.yaml"
STEM = "2026-10-29-two-halts"


def _halt(root: Path, kind: str, stamp: str, revised_at: str = "") -> str:
    d = root / RECORD_DIR_ROOT / f"{kind}-{stamp}" if kind == "ask" else root / RECORD_DIR_ROOT / f"warp-{STEM}-{stamp}"
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    if revised_at:
        (d / rl.NOTES_FILE).write_text(json.dumps({"note": NOTE, "at": revised_at}) + "\n", encoding="utf-8")
        assert hr.mark_revised(d, revised_at)
    return d.name


def _resumed_notes(root: Path, run_id: str) -> list:
    return [e["note"] for e in rl.carried_notes(root, run_id)]


def test_the_older_revised_halt_is_resumed_over_a_newer_unrevised_one(tmp_path):
    revised = _halt(tmp_path, "warp", "20261029T144404-09eb1f", "2026-10-29T15:00:00Z")
    _halt(tmp_path, "warp", "20261029T193744-58fc60")
    assert hr.halted_run_for(tmp_path, SIZING) != revised
    got = hr.halted_run_to_resume(tmp_path, SIZING)
    assert got == revised
    assert _resumed_notes(tmp_path, got) == [NOTE]


def test_a_revised_ask_halt_is_resumed_over_an_older_warp_halt(tmp_path):
    _halt(tmp_path, "warp", "20261029T184714-654207")
    revised = _halt(tmp_path, "ask", "20261029T184721-5c5353", "2026-10-29T19:00:00Z")
    got = hr.halted_run_to_resume(tmp_path, SIZING)
    assert got == revised
    assert _resumed_notes(tmp_path, got) == [NOTE]


def test_the_latest_revise_wins_between_two_revised_halts(tmp_path):
    _halt(tmp_path, "warp", "20261029T100000-aaaaaa", "2026-10-29T12:00:00Z")
    later = _halt(tmp_path, "warp", "20261029T110000-bbbbbb", "2026-10-29T13:00:00Z")
    assert hr.halted_run_to_resume(tmp_path, SIZING) == later


def test_the_older_halt_keeps_winning_when_it_holds_the_latest_revise(tmp_path):
    older = _halt(tmp_path, "warp", "20261029T100000-aaaaaa", "2026-10-29T14:00:00Z")
    _halt(tmp_path, "warp", "20261029T110000-bbbbbb", "2026-10-29T13:00:00Z")
    assert hr.halted_run_to_resume(tmp_path, SIZING) == older


def test_with_no_revise_the_newest_halt_is_still_the_pick(tmp_path):
    _halt(tmp_path, "warp", "20261029T100000-aaaaaa")
    newest = _halt(tmp_path, "warp", "20261029T110000-bbbbbb")
    assert hr.halted_run_to_resume(tmp_path, SIZING) == newest


def test_a_revised_run_that_left_halted_is_not_resumed(tmp_path):
    gone = _halt(tmp_path, "warp", "20261029T100000-aaaaaa", "2026-10-29T14:00:00Z")
    newest = _halt(tmp_path, "warp", "20261029T110000-bbbbbb")
    hr.mark_landed(tmp_path / RECORD_DIR_ROOT / gone, "c" * 40)
    assert hr.halted_run_to_resume(tmp_path, SIZING) == newest


def test_a_retracted_note_is_not_carried(tmp_path):
    root = _repo(tmp_path, record=False)
    _post(root, HALTED_ID)
    d = root / RECORD_DIR_ROOT / HALTED_ID
    with (d / rl.NOTES_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"retracts": "N1", "reason": "applied", "at": "2026-10-29T10:00:00Z"}) + "\n")
    rl.start(root, FRESH)
    assert rl.carry_notes(root, [HALTED_ID], FRESH) == []
