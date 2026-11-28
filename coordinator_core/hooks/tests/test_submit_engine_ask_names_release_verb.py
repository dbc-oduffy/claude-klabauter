"""submit_engine_ask refusals name the release verbs: a running predecessor and a live run both name `dispatch.release_run_token`, and neither offers it for a sizing resubmitted under a newer run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as sea
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.warm import live_runs

PARENT = "ask-parent-20261124T190558-012721"
PRED = "ask-20261124T190558-28aff7"
HELD = "ask-20261124T190558-fdde95"
HELD_SIZING = "state/sizings/2026-11/2026-11-24-ab7224-2-held.yaml"
NEWER = "warp-2026-11-24-ab7224-2-held-20261124T194117-9817f9"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _held_behind_running(root: Path, state: str) -> None:
    rec = c.ParentRecord(
        parent_id=PARENT,
        runs=(
            c.SiblingRun(PRED, "a", status=c.STATUS_RUNNING),
            c.SiblingRun(HELD, "b", after=(PRED,), status=c.STATUS_AFTER, slice_sizing=HELD_SIZING),
        ),
    )
    _write(root, c.parent_record_rel(PARENT), json.dumps(rec.to_json()))
    _write(root, c.status_rel(PRED), json.dumps({"status": state, "parent_id": PARENT}))


def _resubmit(root: Path) -> None:
    _write(root, f"state/warp/{NEWER}/run-stage.json", json.dumps({"stage": "running"}))


def test_running_predecessor_refusal_names_the_release_verbs(tmp_path: Path) -> None:
    _held_behind_running(tmp_path, "running")
    why = sea._slice_deny(tmp_path, (PARENT, HELD))
    assert why is not None and f"Predecessor {PRED} has not landed (running)" in why
    assert f"dispatch.record_halted_run_landed {{run_id: {PRED}, sha}}" in why
    assert f'dispatch.release_run_token {{"run_id": "{PRED}"}}' in why
    assert "no call releases" not in why


def test_queued_predecessor_refusal_also_names_the_verbs(tmp_path: Path) -> None:
    _held_behind_running(tmp_path, "queued")
    why = sea._slice_deny(tmp_path, (PARENT, HELD))
    assert why is not None and "dispatch.record_halted_run_landed" in why and "dispatch.release_run_token" in why
    assert "no call releases" not in why


def test_resubmitted_sizing_is_never_offered_a_release(tmp_path: Path) -> None:
    _held_behind_running(tmp_path, "running")
    _resubmit(tmp_path)
    why = sea._slice_deny(tmp_path, (PARENT, HELD))
    assert why is not None and NEWER in why
    assert "release_run_token" not in why and "record_halted_run_landed" not in why


def test_landed_predecessor_is_not_refused(tmp_path: Path) -> None:
    _held_behind_running(tmp_path, "landed")
    _write(tmp_path, c.status_rel(PRED), json.dumps({"status": "landed", "sha": "abc123def456", "parent_id": PARENT}))
    assert sea._slice_deny(tmp_path, (PARENT, HELD)) is None


def _live_refusal(root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(halted_run, "halted_run_to_resume", lambda *a, **k: HELD)
    monkeypatch.setattr(live_runs, "run_is_live", lambda r, run_id: True)
    payload = {"tool_name": "Workflow", "tool_input": {"name": sea.SUBMIT_WORKFLOW_NAME, "args": {"sizing": HELD_SIZING}}}
    envelope, _ = sea._resume_route(root, HELD_SIZING, payload, {}, None, None)
    return json.dumps(envelope)


def test_live_run_refusal_names_the_release_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = _live_refusal(tmp_path, monkeypatch)
    assert f"Run {HELD} is live" in text
    assert f'dispatch.release_run_token {{\\"run_id\\": \\"{HELD}\\"}}' in text


def test_live_run_refusal_for_a_resubmitted_sizing_offers_no_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _resubmit(tmp_path)
    text = _live_refusal(tmp_path, monkeypatch)
    assert f"Run {HELD} is live" in text and "release_run_token" not in text
