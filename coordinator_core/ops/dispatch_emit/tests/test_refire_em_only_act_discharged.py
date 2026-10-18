"""The refire gate counts an em-discharged derived-em (forbidden sole act) row landed."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.sizing_fire import _drop_em_discharged
from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo
from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path

_PLAN = """---
run_id: fixture
---

## Tasks

```yaml plan-tasks
- id: A1
  title: coded
  disposition: coded
  writes:
    - a.py
- id: B1
  title: build the plugin
  disposition: open
  agent_type: coordinator:executor
  writes: []
  body: |
    Run `pwsh -NoProfile -File scripts/build-plugin.ps1 -EngineVersion 5.8`.
- id: C1
  title: plain agent row
  disposition: open
  writes: []
  body: |
    Read `docs/x.md` and report.
```
"""


def _setup(tmp_path, discharged):
    plan = tmp_path / "plan.md"
    plan.write_text(_PLAN, encoding="utf-8", newline="\n")
    if discharged:
        entries = "".join(f"  {i}:\n    recorded_at: '2026-10-18T00:00:00Z'\n    text: ran it\n" for i in discharged)
        evidence_sidecar_path(plan).write_text(f"em_discharged:\n{entries}", encoding="utf-8", newline="\n")
    text = plan.read_text(encoding="utf-8")
    return plan, text, list(load_rows_memo(text).rows)


def test_a_discharged_derived_em_row_drops_from_open_ids(tmp_path):
    plan, text, rows = _setup(tmp_path, ["B1"])
    assert _drop_em_discharged(plan, text, rows, ["B1", "C1"]) == ["C1"]


def test_an_undischarged_derived_em_row_stays_open(tmp_path):
    plan, text, rows = _setup(tmp_path, [])
    assert _drop_em_discharged(plan, text, rows, ["B1", "C1"]) == ["B1", "C1"]


def test_a_discharge_entry_for_a_plain_agent_row_does_not_drop_it(tmp_path):
    plan, text, rows = _setup(tmp_path, ["C1"])
    assert _drop_em_discharged(plan, text, rows, ["B1", "C1"]) == ["B1", "C1"]
