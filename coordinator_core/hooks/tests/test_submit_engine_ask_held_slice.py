"""submit_engine_ask._slice_deny: a held slice is denied with its hold reason and the supersede release."""

from __future__ import annotations

import json

from coordinator_core.hooks import submit_engine_ask as sea
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c

PARENT = "ask-parent-20261019T155440-64a119"
A = "ask-20261019T155440-aaaaaa"
B = "ask-20261019T155440-bbbbbb"
REASON = "blocked_by hq-01, which is not in this fan-out"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parent(root, runs):
    rec = c.ParentRecord(parent_id=PARENT, runs=tuple(runs))
    _write(root, c.parent_record_rel(PARENT), json.dumps(rec.to_json()))


def test_held_slice_is_denied_with_reason_and_release(tmp_path):
    _parent(tmp_path, [c.SiblingRun(A, "a", status=c.STATUS_HELD, hold_reason=REASON)])
    why = sea._slice_deny(tmp_path, (PARENT, A))
    assert why is not None
    assert REASON in why
    assert f'{{ask, supersedes: "{A}"}}' in why


def test_held_and_superseded_slice_keeps_superseded_text(tmp_path, monkeypatch):
    _parent(tmp_path, [c.SiblingRun(A, "a", status=c.STATUS_HELD, hold_reason=REASON)])
    monkeypatch.setattr(c, "read_superseded_by", lambda root, run_id: B)
    why = sea._slice_deny(tmp_path, (PARENT, A))
    assert why == f"Run {A} is superseded by {B}; it does not start."


def _after_pair(root, pred_status):
    _parent(
        root,
        [c.SiblingRun(A, "a", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", after=(A,), status=c.STATUS_AFTER)],
    )
    _write(root, c.status_rel(A), json.dumps(pred_status))


def test_halted_predecessor_deny_names_record_op(tmp_path):
    _after_pair(tmp_path, {"status": "halted", "parent_id": PARENT})
    why = sea._slice_deny(tmp_path, (PARENT, B))
    assert why is not None
    assert "dispatch.record_halted_run_landed" in why
    assert f'{{ask, supersedes: "{B}"}}' in why


def test_landed_predecessor_with_sha_does_not_deny(tmp_path):
    _after_pair(tmp_path, {"status": "landed", "sha": "abc123def456", "parent_id": PARENT})
    assert sea._slice_deny(tmp_path, (PARENT, B)) is None


def test_queued_predecessor_deny_names_no_phantom_call(tmp_path):
    _parent(
        tmp_path,
        [c.SiblingRun(A, "a", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", after=(A,), status=c.STATUS_AFTER)],
    )
    _write(tmp_path, c.status_rel(A), json.dumps({"status": "queued", "parent_id": PARENT}))
    why = sea._slice_deny(tmp_path, (PARENT, B))
    assert why is not None and f"Predecessor {A} has not landed (queued)" in why
    assert "fire call" not in why


def test_not_started_predecessor_deny_names_its_fire_line(tmp_path):
    _parent(
        tmp_path,
        [c.SiblingRun(A, "a", slice_sizing="state/sizings/a.yaml", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", after=(A,), status=c.STATUS_AFTER)],
    )
    why = sea._slice_deny(tmp_path, (PARENT, B))
    assert why is not None and "not started" in why
    assert "engine-ask {sizing: state/sizings/a.yaml}" in why
    assert "fire call" not in why
