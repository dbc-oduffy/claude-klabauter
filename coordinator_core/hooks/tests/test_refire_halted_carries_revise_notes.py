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


def test_a_retracted_note_is_not_carried(tmp_path):
    root = _repo(tmp_path, record=False)
    _post(root, HALTED_ID)
    d = root / RECORD_DIR_ROOT / HALTED_ID
    with (d / rl.NOTES_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"retracts": "N1", "reason": "applied", "at": "2026-10-29T10:00:00Z"}) + "\n")
    rl.start(root, FRESH)
    assert rl.carry_notes(root, [HALTED_ID], FRESH) == []
