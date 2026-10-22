"""ask_stage stages a dispatched draft step for an owed memo-send row whose outbox draft is absent."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.spine_read import memo_draft_fields

PLAN_REL = "docs/plans/2026-10-16-memo.md"
OUTBOX = ".coordinator-local/memo-outbox"


def _plan(extra: str = "  memo_to: other-em\n  performer: em\n") -> str:
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
    return read_stage_reply(tmp_path, _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path))


_DRAFT = "coordinator_core.ops.fleet.memo_draft._classify_receiver_for_draft"


@pytest.fixture(autouse=True)
def _registered_receivers(monkeypatch):
    """Registry-independent: every receiver resolves unless a test says otherwise."""
    monkeypatch.setattr(_DRAFT, lambda to, dry_run: None)


def test_unregistered_receiver_stages_no_draft_step_and_names_the_close_route(monkeypatch, tmp_path):
    monkeypatch.setattr(_DRAFT, lambda to, dry_run: {"rejection_class": "unknown_receiver"})
    reply = _stage(tmp_path, _plan())
    assert [r["id"] for r in reply["rows"]] == ["C1"]
    assert _owed(reply) == [("M1", "memo-send")]
    detail = reply["owed"][0]["detail"]
    assert "'other-em'" in detail
    assert "delivered_via other" in detail
    assert "http(s) disposition_ref" in detail


def test_injected_classifier_decides_the_refusal(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_stage import OwedRow, _memo_draft_steps

    raw = {"id": "M1", "surface": f"{OUTBOX}/topic.md", "memo_to": "ghost-em", "title": "T"}
    owed = [OwedRow(id="M1", reason="memo-send", detail="d")]
    unknown = lambda to, dry_run: {"rejection_class": "unknown_receiver"} if to == "ghost-em" else None
    out, steps = _memo_draft_steps(tmp_path, tmp_path / "run", owed, {"M1": raw}, "", classify_receiver=unknown)
    assert steps == [] and "'ghost-em'" in out[0].detail
    out, steps = _memo_draft_steps(
        tmp_path, tmp_path / "run", owed, {"M1": raw}, "", classify_receiver=lambda to, dry_run: None
    )
    assert [s.id for s in steps] == ["M1-memo-draft"]


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


def test_registered_memo_row_stages_as_an_executor_row_not_owed(tmp_path):
    reply = _stage(tmp_path, _plan(extra="  memo_to: other-em\n"))
    assert {r["id"] for r in reply["rows"]} == {"C1", "M1"}
    assert reply["owed"] == []


def test_unregistered_dispatchable_memo_row_is_demoted_with_its_dependents(monkeypatch, tmp_path):
    monkeypatch.setattr(_DRAFT, lambda to, dry_run: {"rejection_class": "unknown_receiver"})
    plan = _plan(extra="  memo_to: ghost-em\n").replace(
        "    Please do the thing.\n",
        "    Please do the thing.\n- id: D1\n  title: after memo\n  change_kind: code-edit\n"
        "  surface: pkg/d.py\n  writes:\n    - pkg/d.py\n  depends_on:\n    - chunk: M1\n"
        "      gate_kind: output-consumption-runtime\n",
    )
    reply = _stage(tmp_path, plan)
    assert [r["id"] for r in reply["rows"]] == ["C1"]
    assert _owed(reply) == [("M1", "memo-send"), ("D1", "withheld_by_owed_dependency")]
    assert "delivered_via other" in reply["owed"][0]["detail"]
    assert "M1" in reply["owed"][1]["detail"]


def test_draft_step_without_dependencies_stays_at_wave_zero(tmp_path):
    step = next(r for r in _stage(tmp_path, _plan())["rows"] if r["id"] == "M1-memo-draft")
    assert (step["deps"], step["wave"]) == ([], 0)
