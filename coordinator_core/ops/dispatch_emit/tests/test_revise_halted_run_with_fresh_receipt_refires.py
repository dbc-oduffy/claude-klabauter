"""workflow.revise on a halted run whose stage receipt is fresh takes the refire branch, and a note naming a
file widens the footprint with no edit verb; the same note on a live run keeps the edit-verb rule."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halted_run, requirements_ledger as rl, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.warm import live_runs

RUN = "ask-20261116T000000-abc123"
REL = "state/sizings/2026-11-16-halted-revise.yaml"
PLAN_REL = "docs/plans/2026-11-16-halted-revise.md"
MOD = "pkg/land_run.py"
OTHER = "pkg/close_background.py"
TEST = "pkg/tests/test_land_run.py"
NOTE = f"The landed_paths wiring widens to {OTHER} and {TEST}."

PLAN = f"""---
deliverable_id: dlv-halted-abc123
---
# Halted revise

## Problem

One row.

## Tasks

```yaml plan-tasks
- id: C1
  title: land
  change_kind: code-edit
  surface: {MOD}
  writes:
    - {MOD}
```
"""


@pytest.fixture
def run(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    for rel in (MOD, OTHER, TEST):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")
    (tmp_path / PLAN_REL).parent.mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}], "plan": PLAN_REL,
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    write_run_scripts(tmp_path, RUN)
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    record = tmp_path / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True, exist_ok=True)
    manifest = {"rows": [{"id": "C1", "brief_path": f"scratch/warp/{RUN}/briefs/C1.md", "writes": [MOD]}]}
    (record / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(live_runs, "run_is_live", lambda *_a, **_k: True)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: False)
    return tmp_path


def _halt(root):
    halted_run.write_halt_marker(
        root / RECORD_DIR_ROOT / RUN, halted="criterion_not_met", sizing_rel=REL, plan_rel=PLAN_REL
    )


def _plan_writes(root) -> list:
    return rl._plan_rows(root, REL)[0]["writes"]


def test_halted_run_with_fresh_receipt_replies_refire_not_briefed(run):
    _halt(run)
    reply = rl._handler({"run_id": RUN, "note": NOTE}, repo_root=run)
    assert reply["ok"] is True and reply["run_live"] is False
    assert "briefed" not in reply and "rows already briefed" not in reply.get("note", "")
    assert reply["next"]["kind"] == "refire"


def test_halted_note_widens_on_a_named_file_without_an_edit_verb(run):
    _halt(run)
    reply = rl._handler({"run_id": RUN, "note": NOTE}, repo_root=run)
    widened = {p for w in reply["footprint_widened"] for p in w["paths"]}
    assert widened == {OTHER, TEST}
    assert set(_plan_writes(run)) >= {MOD, OTHER, TEST}
    assert [w["row"] for w in reply["footprint_widened"]] == ["C1"]


def test_live_run_with_a_citing_note_stays_live_and_unwidened(run):
    reply = rl._handler({"run_id": RUN, "note": f"The landed_paths wiring touches {OTHER} and {TEST}."}, repo_root=run)
    assert reply["run_live"] is True
    assert "footprint_widened" not in reply
    assert _plan_writes(run) == [MOD]


def test_another_runs_live_token_for_the_sizing_does_not_count_on_a_halted_run(run, monkeypatch):
    _halt(run)
    monkeypatch.setattr(
        run_token, "live_records", lambda *_a, **_k: [{"run_id": "ask-other-000000", "sizing_rel": REL}]
    )
    monkeypatch.setattr(rl, "_token_not_a_runner", lambda *_a, **_k: False)
    assert rl._handler({"run_id": RUN, "note": NOTE}, repo_root=run)["run_live"] is False
