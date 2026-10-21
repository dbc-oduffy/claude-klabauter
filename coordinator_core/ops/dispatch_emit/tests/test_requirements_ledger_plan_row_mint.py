"""ask_stage seeds `plan-row:<id>` register rows from the plan's rows when the ledger would be empty."""

from __future__ import annotations

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import EMPTY_REGISTER_REASON, PLAN_ROW_ANCHOR_PREFIX
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

RUN = "ask-20261021T000000-abc123"
PLAN_REL = "docs/plans/2026-10-21-mint.md"
SIZING_REL = "state/sizings/2026-10-21-mint.yaml"

PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
- id: C3
  title: third
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
```
"""


def _sizing(register: dict | None) -> dict:
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    }
    if register is not None:
        doc["requirement_register"] = register
    return doc


def _row(rid: str, status: str = "open", anchor: str = "ask") -> dict:
    return {"id": rid, "source_text": f"requirement {rid}", "anchor": anchor, "surface": "internal", "status": status}


def _setup(tmp_path, register):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(_sizing(register), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)


def _stage(tmp_path):
    params = {"run_id": RUN, "plan_path": PLAN_REL, "commit_sizing_path": SIZING_REL}
    return read_stage_reply(tmp_path, _handler(params, repo_root=tmp_path))


def _rows(tmp_path):
    return rr.read_register((tmp_path / SIZING_REL).read_text(encoding="utf-8")).rows


def test_empty_register_is_seeded_from_every_plan_row(tmp_path):
    _setup(tmp_path, None)
    reply = _stage(tmp_path)
    assert "error" not in reply
    rows = _rows(tmp_path)
    assert [r["anchor"] for r in rows] == [f"{PLAN_ROW_ANCHOR_PREFIX}C{n}" for n in (1, 2, 3)]
    assert [r["status"] for r in rows] == ["open"] * 3
    assert rows[1]["source_text"] == "Plan row C2: second"
    assert reply["ledger_item_ids"] == [r["id"] for r in rows]
    assert reply["rulable_ids"] == [r["id"] for r in rows]


def test_second_stage_adds_no_row(tmp_path):
    _setup(tmp_path, None)
    _stage(tmp_path)
    first = _rows(tmp_path)
    _stage(tmp_path)
    assert _rows(tmp_path) == first


def test_register_with_an_ask_row_gets_no_plan_rows(tmp_path):
    _setup(tmp_path, {"rows": [_row("R1")]})
    reply = _stage(tmp_path)
    assert [r["id"] for r in _rows(tmp_path)] == ["R1"]
    assert reply["ledger_item_ids"] == ["R1"]


def test_fully_withheld_register_gets_no_plan_rows(tmp_path):
    ruling = {"source": "pm", "quote": "q", "ref": f"workflow.revise:{RUN}:out_of_scope", "on": "2026-10-21"}
    _setup(tmp_path, {"rows": [{**_row("R1", "deferred"), "ruling": ruling}]})
    reply = _stage(tmp_path)
    assert [r["id"] for r in _rows(tmp_path)] == ["R1"]
    assert reply["ledger_item_ids"] == [] and reply["withheld_ids"] == ["R1"]


def test_plan_row_origin_is_read_from_the_anchor(tmp_path):
    _setup(tmp_path, None)
    _stage(tmp_path)
    items = rl._ledger_of(_rows(tmp_path), False, SIZING_REL).items
    assert [(i.origin, i.at) for i in items] == [("plan-row", "C1"), ("plan-row", "C2"), ("plan-row", "C3")]


def test_judge_on_a_sizing_bound_empty_ledger_names_the_sizing():
    out = rl.judge(rl.Ledger((), False, SIZING_REL), {"items": []})
    assert out["pass"] is False
    reason = out["failures"][0]["reason"]
    assert EMPTY_REGISTER_REASON in reason and SIZING_REL in reason


def test_judge_on_an_unbound_empty_ledger_keeps_the_bare_reason():
    out = rl.judge(rl.Ledger((), False), {"items": []})
    assert out["failures"] == [{"id": None, "reason": EMPTY_REGISTER_REASON}]
