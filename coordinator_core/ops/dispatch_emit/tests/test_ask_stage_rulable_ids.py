"""ask_stage fills StageManifest.rulable_ids from the run sizing's register: the ids workflow.revise out_of_scope accepts."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import _handler

RUN = "ask-20261016T000000-abc123"
PLAN_REL = "docs/plans/2026-10-16-rulable.md"
SIZING_REL = "state/sizings/2026-10-16-rulable.yaml"

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
- id: X1
  title: gated
  change_kind: code-edit
  surface: pkg/g.py
  writes:
    - pkg/g.py
  external_gate:
    - owner_repo: other
      requires: landed-work
```
"""


def _row(rid: str, status: str) -> dict:
    return {"id": rid, "source_text": f"requirement {rid}", "anchor": "ask", "surface": "internal", "status": status}


def _sizing(register: dict | None) -> dict:
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    }
    if register is not None:
        doc["requirement_register"] = register
    return doc


def _stage(tmp_path, sizing: dict | None, *, run_token: bool = True):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    params = {"run_id": RUN, "plan_path": PLAN_REL}
    if sizing is not None:
        (tmp_path / "state" / "sizings").mkdir(parents=True)
        (tmp_path / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
        params["commit_sizing_path"] = SIZING_REL
        rl.start(tmp_path, RUN, all_of_pm_ask=False)
    return _handler(params, repo_root=tmp_path)


@pytest.fixture
def staged(tmp_path):
    register = {"rows": [_row("R1", "open"), _row("R2", "open"), _row("R3", "met")]}
    return tmp_path, _stage(tmp_path, _sizing(register))


def test_manifest_lists_the_open_register_rows_and_not_a_gated_plan_row(staged):
    _, reply = staged
    assert "error" not in reply
    assert StageManifest.from_json(reply).rulable_ids == ("R1", "R2")
    assert "X1" not in reply["rulable_ids"]
    assert [g["id"] for g in reply["gated"]] == ["X1"]


def test_every_listed_id_is_accepted_by_revise(staged):
    root, reply = staged
    ids = reply["rulable_ids"]
    rl.revise(root, RUN, verbatim="R2 is out of scope", out_of_scope=[ids[1]])


def test_revise_still_refuses_a_gated_plan_row_id(staged):
    root, _ = staged
    with pytest.raises(rl.LedgerRefused, match="out_of_scope names no register row: X1"):
        rl.revise(root, RUN, verbatim="X1 is out of scope", out_of_scope=["X1"])


def test_no_sizing_yields_no_rulable_ids(tmp_path):
    reply = _stage(tmp_path, None)
    assert "error" not in reply
    assert reply["rulable_ids"] == []


def test_a_sizing_without_a_register_yields_no_rulable_ids(tmp_path):
    reply = _stage(tmp_path, _sizing(None))
    assert "error" not in reply
    assert reply["rulable_ids"] == []
