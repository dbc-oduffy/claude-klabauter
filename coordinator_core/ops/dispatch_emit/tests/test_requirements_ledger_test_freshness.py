"""judge fails a met item whose tests_ran_at predates the last edit of the writes of the spine rows claiming it."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ledger_test_freshness import STALE_TESTS_REASON, TESTS_RAN_AT_KEY
from coordinator_core.ops.dispatch_emit.tests.test_ledger_test_freshness import (  # noqa: F401  (autouse fixture)
    LEG,
    _hash_without_git,
    _raw_blob,
)

pytestmark = pytest.mark.complete

EDIT = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

PLAN = """---
title: "plan"
---

# plan

## Tasks

```yaml plan-tasks
- id: C1
  title: the work
  writes:
    - a.py
  register_rows: ["R1"]
  disposition: open
- id: C2
  title: other work
  writes:
    - b.py
  register_rows: ["R2"]
  disposition: open
- id: C3
  title: dropped work
  writes:
    - gone.py
  register_rows: ["R1"]
  disposition: wont_do
```
"""


def _ledger(*ids):
    return rl.Ledger(
        items=tuple(rl.Item(i, f"text {i}", "pm", "2026-11-16T00:00:00Z", anchor="pm_verbatims") for i in ids),
        all_of_pm_ask=False,
    )


def _touch(root: Path, name: str, when: datetime = EDIT) -> None:
    path = root / name
    path.write_text("x\n", encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))


def _met(rid: str, ran: str | None) -> dict:
    row = {"id": rid, "status": "met", "evidence": "verified"}
    if ran is not None:
        row[TESTS_RAN_AT_KEY] = ran
    return row


def _stale_ids(out: dict) -> list:
    return [f["id"] for f in out["failures"] if f["reason"].startswith(STALE_TESTS_REASON)]


def test_a_met_item_with_tests_after_the_last_edit_passes(tmp_path):
    _touch(tmp_path, "a.py")
    ran = (EDIT + timedelta(minutes=1)).isoformat()
    out = rl.judge(_ledger("R1"), {"items": [_met("R1", ran)]}, repo_root=tmp_path, plan_text=PLAN)
    assert out["pass"] is True and out["failures"] == []


def test_a_met_item_with_tests_before_the_last_edit_fails(tmp_path):
    _touch(tmp_path, "a.py")
    ran = (EDIT - timedelta(minutes=1)).isoformat()
    out = rl.judge(_ledger("R1"), {"items": [_met("R1", ran)]}, repo_root=tmp_path, plan_text=PLAN)
    assert not out["pass"] and _stale_ids(out) == ["R1"]


def test_a_met_item_without_tests_ran_at_fails(tmp_path):
    _touch(tmp_path, "a.py")
    out = rl.judge(_ledger("R1"), {"items": [_met("R1", None)]}, repo_root=tmp_path, plan_text=PLAN)
    assert _stale_ids(out) == ["R1"]


def test_only_the_writes_of_the_rows_claiming_the_item_count(tmp_path):
    _touch(tmp_path, "a.py", EDIT - timedelta(hours=1))
    _touch(tmp_path, "b.py")
    ran = (EDIT - timedelta(minutes=1)).isoformat()
    verdict = {"items": [_met("R1", ran), _met("R2", ran)]}
    out = rl.judge(_ledger("R1", "R2"), verdict, repo_root=tmp_path, plan_text=PLAN)
    assert _stale_ids(out) == ["R2"]


def test_a_non_claiming_row_writes_do_not_count(tmp_path):
    _touch(tmp_path, "a.py", EDIT - timedelta(hours=1))
    _touch(tmp_path, "gone.py")
    ran = (EDIT - timedelta(minutes=1)).isoformat()
    out = rl.judge(_ledger("R1"), {"items": [_met("R1", ran)]}, repo_root=tmp_path, plan_text=PLAN)
    assert out["pass"] is True


def test_no_existing_write_skips_the_check(tmp_path):
    out = rl.judge(_ledger("R1"), {"items": [_met("R1", None)]}, repo_root=tmp_path, plan_text=PLAN)
    assert out["pass"] is True


def test_no_repo_root_or_plan_text_skips_the_check(tmp_path):
    _touch(tmp_path, "a.py")
    verdict = {"items": [_met("R1", None)]}
    assert rl.judge(_ledger("R1"), verdict, plan_text=PLAN)["pass"] is True
    assert rl.judge(_ledger("R1"), verdict, repo_root=tmp_path)["pass"] is True


def test_a_criterion_credited_item_is_held_to_all_live_spine_writes(tmp_path):
    _touch(tmp_path, "a.py", EDIT - timedelta(hours=1))
    _touch(tmp_path, "b.py")
    ran = (EDIT - timedelta(minutes=1)).isoformat()
    verdict = {"items": [{"id": "criterion", "status": "met", "evidence": "verified", TESTS_RAN_AT_KEY: ran}]}
    out = rl.judge(_ledger("R1"), verdict, repo_root=tmp_path, plan_text=PLAN)
    assert _stale_ids(out) == ["R1"]
    fresh = {"items": [{
        "id": "criterion", "status": "met", "evidence": "verified",
        TESTS_RAN_AT_KEY: (EDIT + timedelta(minutes=1)).isoformat(),
    }]}
    assert rl.judge(_ledger("R1"), fresh, repo_root=tmp_path, plan_text=PLAN)["pass"] is True


REL = "state/sizings/2026-11-23-fresh.yaml"
RUN = "ask-20261123T000000-abc123"


def test_the_terminal_commit_ledger_gate_refuses_stale_tests(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    ids = [i.id for i in rl.read_ledger(tmp_path, run_dir).items]
    plan = PLAN.replace('["R1"]', f'["{ids[0]}"]')
    _touch(tmp_path, "a.py")

    def gate(ran):
        verdict = {"items": [_met(i, ran) for i in ids]}
        return terminal_commit_ledger._ledger_gate(tmp_path, run_dir, verdict, None, plan_text=plan)[0]

    refusal = gate((EDIT - timedelta(minutes=1)).isoformat())
    assert refusal is not None and refusal["refused"] == "ledger-unmet"
    assert any(f["reason"].startswith(STALE_TESTS_REASON) for f in refusal["ledger_failures"])
    assert gate((EDIT + timedelta(minutes=1)).isoformat()) is None

    _leg(tmp_path, RUN, LEG, rev=("a.py",))
    read = rl.read_ledger(tmp_path, run_dir)
    assert read.run_id == RUN
    verdict = {"items": [_met(i, None) for i in ids]}
    _touch(tmp_path, "a.py", LEG + timedelta(hours=1))
    assert rl.judge(read, verdict, repo_root=tmp_path, plan_text=plan)["pass"] is True
    (tmp_path / "a.py").write_text("changed\n", encoding="utf-8")
    os.utime(tmp_path / "a.py", ((LEG + timedelta(hours=1)).timestamp(),) * 2)
    refusal = gate(None)
    assert refusal is not None and refusal["refused"] == "ledger-unmet"


def _leg(root: Path, run_id: str, measured_at: datetime, rev=()) -> None:
    run_dir = root / RECORD_DIR_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "test-leg.json").write_text(json.dumps({"measured_at": measured_at.isoformat()}), encoding="utf-8")
    record = run_dir / "measured-rev.json"
    record.write_text(json.dumps({"blobs": {rel: _raw_blob(root, rel) for rel in rev}}), encoding="utf-8")
    os.utime(record, (EDIT.timestamp(), EDIT.timestamp()))


def test_a_met_item_without_tests_ran_at_passes_on_the_runs_fresh_test_leg(tmp_path):
    _touch(tmp_path, "a.py")
    _leg(tmp_path, RUN, LEG)
    ledger = rl.Ledger(_ledger("R1").items, False, run_id=RUN)
    out = rl.judge(ledger, {"items": [_met("R1", None)]}, repo_root=tmp_path, plan_text=PLAN)
    assert out["pass"] is True and out["failures"] == []
    explicit = rl.judge(_ledger("R1"), {"items": [_met("R1", None)]}, repo_root=tmp_path, plan_text=PLAN, run_id=RUN)
    assert explicit["pass"] is True


def test_a_peer_touch_after_the_leg_passes_and_an_own_content_change_fails(tmp_path):
    _touch(tmp_path, "a.py")
    _leg(tmp_path, RUN, LEG, rev=("a.py",))
    ledger = rl.Ledger(_ledger("R1").items, False, run_id=RUN)
    verdict = {"items": [_met("R1", None)]}
    _touch(tmp_path, "a.py", LEG + timedelta(hours=1))
    assert rl.judge(ledger, verdict, repo_root=tmp_path, plan_text=PLAN)["pass"] is True
    (tmp_path / "a.py").write_text("changed\n", encoding="utf-8")
    os.utime(tmp_path / "a.py", ((LEG + timedelta(hours=1)).timestamp(),) * 2)
    assert _stale_ids(rl.judge(ledger, verdict, repo_root=tmp_path, plan_text=PLAN)) == ["R1"]


def test_without_a_run_the_absent_stamp_is_still_refused(tmp_path):
    _touch(tmp_path, "a.py")
    _leg(tmp_path, RUN, LEG)
    out = rl.judge(_ledger("R1"), {"items": [_met("R1", None)]}, repo_root=tmp_path, plan_text=PLAN)
    assert _stale_ids(out) == ["R1"]
