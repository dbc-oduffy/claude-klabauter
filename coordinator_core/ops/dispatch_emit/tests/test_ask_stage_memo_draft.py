"""ask_stage stages a dispatched draft step for an owed memo-send row whose outbox draft is absent."""

from __future__ import annotations

import pytest

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


def test_row_without_a_receiver_is_refused_at_read(tmp_path):
    # read_spine refuses an unsent memo-send row with no memo_to before staging runs.
    from coordinator_core.ops.dispatch_emit.spine_read import MemoRowNoReceiverError

    with pytest.raises(MemoRowNoReceiverError):
        _stage(tmp_path, _plan(extra=""))


def test_draft_fields_refuse_a_bad_kind_and_a_missing_slug():
    ok, _ = memo_draft_fields({"surface": f"{OUTBOX}/t.md", "memo_to": "x", "title": "T"})
    assert ok == {"slug": "t", "to": "x", "title": "T", "kind": "ask"}
    assert memo_draft_fields({"surface": f"{OUTBOX}/t.md", "memo_to": "x", "memo_kind": "zzz"})[0] is None
    assert memo_draft_fields({"surface": "pkg/a.py", "memo_to": "x"})[0] is None
    assert memo_draft_fields({"receipt": "../x", "memo_to": "x"})[0] is None
    assert memo_draft_fields({"receipt": "a b.md", "memo_to": "x"})[0] is None


def test_draft_step_inherits_the_owed_rows_staged_dependency(tmp_path):
    plan = _plan().replace(
        "  writes: []\n  memo_to", "  writes: []\n  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n  memo_to"
    )
    reply = _stage(tmp_path, plan)
    code = next(r for r in reply["rows"] if r["id"] == "C1")
    step = next(r for r in reply["rows"] if r["id"] == "M1-memo-draft")
    assert step["deps"] == ["C1"]
    assert step["wave"] == code["wave"] + 1


def test_draft_step_without_dependencies_stays_at_wave_zero(tmp_path):
    step = next(r for r in _stage(tmp_path, _plan())["rows"] if r["id"] == "M1-memo-draft")
    assert (step["deps"], step["wave"]) == ([], 0)
