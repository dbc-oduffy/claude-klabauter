"""ask_stage fills StageManifest.ledger_item_ids / withheld_ids from the run sizing's register via requirements_ledger.ledger_ids."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

RUN = "ask-20261016T000000-abc124"
PLAN_REL = "docs/plans/2026-10-16-ledger-ids.md"
SIZING_REL = "state/sizings/2026-10-16-ledger-ids.yaml"

PLAN = """---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: U1
  title: ungated
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
```
"""


def _row(rid: str, status: str = "open", ref: str | None = None) -> dict:
    row = {"id": rid, "source_text": f"requirement {rid}", "anchor": "ask", "surface": "internal", "status": status}
    if ref:
        row["status"] = "deferred"
        row["ruling"] = {"ref": ref, "on": "2026-10-20"}
    return row


def _stage(tmp_path, rows: list[dict]):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    sizing = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "requirement_register": {"rows": rows},
    }
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    return read_stage_reply(
        tmp_path,
        _handler({"run_id": RUN, "plan_path": PLAN_REL, "commit_sizing_path": SIZING_REL}, repo_root=tmp_path),
    )


def test_every_row_ruled_out_stages_empty_items_and_all_withheld(tmp_path):
    ruling = f"workflow.revise:{RUN}:out_of_scope"
    reply = _stage(tmp_path, [_row("R1", ref=ruling), _row("R2", ref=ruling)])
    assert "error" not in reply
    assert reply["ledger_item_ids"] == []
    assert reply["withheld_ids"] == ["R1", "R2"]
    manifest = StageManifest.from_json(reply)
    assert manifest.ledger_item_ids == ()
    assert manifest.withheld_ids == ("R1", "R2")


def test_open_rows_are_items_and_ruled_out_rows_are_withheld(tmp_path):
    reply = _stage(tmp_path, [_row("R1"), _row("R2", ref=f"workflow.revise:{RUN}:out_of_scope")])
    assert reply["ledger_item_ids"] == ["R1"]
    assert reply["withheld_ids"] == ["R2"]
    assert (reply["ledger_item_ids"], reply["withheld_ids"]) == tuple(list(t) for t in rl.ledger_ids([_row("R1"), _row("R2", ref=f"workflow.revise:{RUN}:out_of_scope")]))


def test_manifest_json_without_the_keys_reads_empty():
    manifest = StageManifest.from_json({"run_dir": "d", "rows": [], "review_declared_paths": [], "marker_path": "m"})
    assert manifest.ledger_item_ids == ()
    assert manifest.withheld_ids == ()
