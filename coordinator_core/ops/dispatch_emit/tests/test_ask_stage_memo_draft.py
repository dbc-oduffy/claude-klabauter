"""ask_stage stages a dispatched draft step for an owed memo-send row whose outbox draft is absent."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.spine_read import memo_draft_fields

PLAN_REL = "docs/plans/2026-10-16-memo.md"
OUTBOX = ".coordinator-local/memo-outbox"


def _plan(extra: str = "  memo_to: other-em\n") -> str:
    return f"""---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: C1
  title: code
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: M1
  title: tell other
  change_kind: doc-edit
  surface: {OUTBOX}/topic.md
  writes: []
{extra}  body: |
    Please do the thing.
```
"""


def _stage(tmp_path, plan: str):
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)


def _owed(reply):
    return [(o["id"], o["reason"]) for o in reply["owed"]]


def test_absent_draft_stages_a_draft_step_and_keeps_the_row_owed(tmp_path):
    reply = _stage(tmp_path, _plan())
    step = next(r for r in reply["rows"] if r["id"] == "M1-memo-draft")
    assert step["writes"] == [f"{OUTBOX}/topic.md"]
    assert _owed(reply) == [("M1", "memo-send")]
    brief = (tmp_path / step["brief_path"]).read_text(encoding="utf-8")
    assert "cross-repo-memo draft topic --to other-em --kind ask" in brief
    assert "Please do the thing." in brief


def test_existing_draft_yields_owed_only(tmp_path):
    (tmp_path / OUTBOX).mkdir(parents=True)
    (tmp_path / OUTBOX / "topic.md").write_text("draft\n", encoding="utf-8")
    reply = _stage(tmp_path, _plan())
    assert [r["id"] for r in reply["rows"]] == ["C1"]
    assert _owed(reply) == [("M1", "memo-send")]


def test_sent_receipt_yields_neither(tmp_path):
    (tmp_path / OUTBOX / "sent").mkdir(parents=True)
    (tmp_path / OUTBOX / "sent" / "topic.md").write_text("sent\n", encoding="utf-8")
    reply = _stage(tmp_path, _plan())
    assert [r["id"] for r in reply["rows"]] == ["C1"]
    assert reply["owed"] == []


def test_row_without_a_receiver_gets_no_step_and_says_why(tmp_path):
    reply = _stage(tmp_path, _plan(extra=""))
    assert [r["id"] for r in reply["rows"]] == ["C1"]
    assert _owed(reply) == [("M1", "memo-send")]
    assert "no receiver" in reply["owed"][0]["detail"]


def test_draft_fields_refuse_a_bad_kind_and_a_missing_slug():
    ok, _ = memo_draft_fields({"surface": f"{OUTBOX}/t.md", "memo_to": "x", "title": "T"})
    assert ok == {"slug": "t", "to": "x", "title": "T", "kind": "ask"}
    assert memo_draft_fields({"surface": f"{OUTBOX}/t.md", "memo_to": "x", "memo_kind": "zzz"})[0] is None
    assert memo_draft_fields({"surface": "pkg/a.py", "memo_to": "x"})[0] is None
    assert memo_draft_fields({"receipt": "../x", "memo_to": "x"})[0] is None
    assert memo_draft_fields({"receipt": "a b.md", "memo_to": "x"})[0] is None
