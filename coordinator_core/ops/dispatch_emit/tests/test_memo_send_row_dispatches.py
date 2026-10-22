"""A memo-send row with depends_on stages as an executor row, its dependents follow, and the guard admits a subagent's send."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/2026-10-22-memo-dispatch.md"
OUTBOX = ".coordinator-local/memo-outbox"
_CLASSIFIER = "coordinator_core.ops.fleet.memo_draft._classify_receiver_for_draft"

PLAN = f"""---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: A
  title: code
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: M
  title: tell other
  change_kind: doc-edit
  kind: memo-send
  surface: {OUTBOX}/topic.md
  memo_to: other-em
  writes: []
  depends_on:
    - chunk: A
      gate_kind: output-consumption-runtime
      note: fixture edge
  body: |
    Please do the thing.
- id: B
  title: after the memo
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  depends_on:
    - chunk: M
      gate_kind: output-consumption-runtime
      note: fixture edge
- id: E
  title: em sends
  change_kind: doc-edit
  kind: memo-send
  performer: em
  surface: {OUTBOX}/em-topic.md
  memo_to: other-em
  writes: []
  body: |
    The EM sends this one.
```
"""


def _stage(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    return read_stage_reply(tmp_path, _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path))


def _wave_of(reply, row_id):
    return next(r for r in reply["rows"] if r["id"] == row_id)["wave"]


def _row(reply, row_id):
    return next(r for r in reply["rows"] if r["id"] == row_id)


def _owed(reply):
    return {o["id"]: o["reason"] for o in reply["owed"]}


def test_registered_receiver_stages_the_memo_row_as_an_executor_row(monkeypatch, tmp_path):
    monkeypatch.setattr(_CLASSIFIER, lambda to, dry_run: None)
    reply = _stage(tmp_path)

    m = _row(reply, "M")
    assert m["agent_type"] == "coordinator:executor"
    assert _wave_of(reply, "M") == _wave_of(reply, "A") + 1
    assert list(m["deps"]) == ["A"]
    brief = (tmp_path / m["brief_path"]).read_text(encoding="utf-8")
    assert "cross-repo-memo send topic" in brief
    assert f"{OUTBOX}/sent/topic.md" in brief

    assert _wave_of(reply, "B") > _wave_of(reply, "M")
    assert _owed(reply) == {"E": "memo-send"}


def test_unregistered_receiver_owes_the_memo_row_and_its_dependent(monkeypatch, tmp_path):
    monkeypatch.setattr(_CLASSIFIER, lambda to, dry_run: {"rejection_class": "unknown_receiver"})
    reply = _stage(tmp_path)

    assert {r["id"] for r in reply["rows"]} == {"A"}
    assert _owed(reply) == {"M": "memo-send", "B": "withheld_by_owed_dependency", "E": "memo-send"}


_SEND_OP = "memo" + "." + "send"


@pytest.fixture
def _subagent(monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda git_root, agent_id: "coordinator:executor")


def test_guard_admits_a_subagent_memo_send_invoke(_subagent):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f'coordinator-invoke {_SEND_OP} \'{{"topic": "x"}}\''},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    assert guard.check(payload) is None
