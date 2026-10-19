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


def test_non_held_after_slice_deny_text_is_unchanged(tmp_path):
    _parent(
        tmp_path,
        [c.SiblingRun(A, "a", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", after=(A,), status=c.STATUS_AFTER)],
    )
    _write(tmp_path, c.status_rel(A), json.dumps({"status": "queued", "parent_id": PARENT}))
    why = sea._slice_deny(tmp_path, (PARENT, B))
    assert why == f"Predecessor {A} has not landed (queued); its close names this run's fire call."
