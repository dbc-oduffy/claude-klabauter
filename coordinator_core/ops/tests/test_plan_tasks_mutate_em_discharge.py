"""Tests for the plan.tasks.mutate ``em-discharge`` verb and ``read_em_discharges``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.plan_tasks_mutate import (
    EM_DISCHARGE_KEY,
    _handler,
    evidence_sidecar_path,
    read_em_discharges,
    read_row_evidence,
)
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Discharge Plan"
status: ready
---

# Discharge Plan

## Tasks

```yaml plan-tasks
- id: E1
  title: EM step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: open
  body: |
    Do it.
- id: M1
  title: Send memo
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  kind: memo-send
  body: |
    Send.
- id: O1
  title: Operator step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  execution_mode: operator
  body: |
    Operator.
- id: X1
  title: Executor step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  body: |
    Code.
- id: D1
  title: Already closed
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: wont_do
  body: |
    Closed.
```

## Trailer
"""

_B1_BODY = """\
  body: |
    Run `pwsh -NoProfile -File scripts/build-plugin.ps1 -EngineVersion 5.8`.
"""


def _derived_plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    rows = f"""\
- id: B1
  title: Build plugin
  change_kind: verification
  surface: scripts/build-plugin.ps1
  writes: []
  agent_type: coordinator:executor
  queue_scope: project
  disposition: open
{_B1_BODY}- id: B2
  title: Build and author
  change_kind: code-edit
  surface: scripts/build-plugin.ps1
  writes: [x.cpp]
  queue_scope: project
  disposition: open
{_B1_BODY}- id: B3
  title: Operator build
  change_kind: verification
  surface: scripts/build-plugin.ps1
  writes: []
  queue_scope: project
  execution_mode: operator
  disposition: open
{_B1_BODY}"""
    plan.write_text(
        "---\ntitle: P\nstatus: ready\n---\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n",
        encoding="utf-8",
        newline="",
    )
    return repo, plan


def _plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    plan.write_text(_PLAN, encoding="utf-8", newline="")
    return repo, plan


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {"verb": "em-discharge", "plan_path": str(plan), "id": "E1", "text": "probe done"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def test_discharge_writes_key_and_leaves_plan_bytes(tmp_path):
    repo, plan = _plan(tmp_path)
    before = plan.read_bytes()
    result = _call(repo, plan)
    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert plan.read_bytes() == before
    got = read_em_discharges(plan)
    assert set(got) == {"E1"}
    assert got["E1"]["text"] == "probe done"
    assert got["E1"]["recorded_at"]
    assert read_row_evidence(plan) == {}
    assert EM_DISCHARGE_KEY == "em_discharged"


def test_second_discharge_is_noop_not_overwrite(tmp_path):
    repo, plan = _plan(tmp_path)
    _call(repo, plan)
    first = read_em_discharges(plan)["E1"]
    result = _call(repo, plan, text="different")
    assert result["exit_code"] == 0 and result["applied"] is False
    assert read_em_discharges(plan)["E1"] == first


def test_xs_spine_under_state_warp_discharges(tmp_path):
    repo, plan = _plan(tmp_path)
    spine = repo / "state" / "warp" / "ask-1" / "s.spine.md"
    spine.parent.mkdir(parents=True)
    spine.write_text(_PLAN, encoding="utf-8", newline="")
    result = _call(repo, spine)
    assert result["exit_code"] == 0 and result["applied"] is True, result
    assert set(read_em_discharges(spine)) == {"E1"}


def test_plan_path_outside_plans_and_state_warp_still_refused(tmp_path):
    repo, plan = _plan(tmp_path)
    stray = repo / "state" / "other" / "s.spine.md"
    stray.parent.mkdir(parents=True)
    stray.write_text(_PLAN, encoding="utf-8", newline="")
    result = _call(repo, stray)
    assert result["exit_code"] == 1 and "escapes" in result["error"]
    assert not evidence_sidecar_path(stray).exists()


def test_detection_target_claude_md_mints_no_em_row_but_edit_target_does(tmp_path):
    from coordinator_core.ops.dispatch_emit.sizing_xs_mint import mint_xs_spine

    (tmp_path / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x\n", encoding="utf-8")

    def spine(text):
        sizing = {
            "intent": "i",
            "exit_criterion": {"statement": "s"},
            "requirement_register": {
                "rows": [{"id": "R1", "source_text": text, "status": "open", "anchor": "relayed"}]
            },
        }
        return mint_xs_spine(
            sizing, sizing_rel="state/sizings/s.yaml", writes=["src/x.py"], out_dir=tmp_path / "out", repo_root=tmp_path
        )[0]

    detect = "void the cell if any ancestor of its sandbox holds a CLAUDE.md"
    assert "performer: em" not in spine(detect)
    assert "performer: em" in spine("edit CLAUDE.md")


@pytest.mark.parametrize("row_id", ["NOPE", "M1", "O1", "X1", "D1"])
def test_refusals_write_nothing(tmp_path, row_id):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, id=row_id)
    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert not evidence_sidecar_path(plan).exists()


@pytest.mark.parametrize("extra", [{"id": ""}, {"text": ""}, {"text": "   "}])
def test_empty_id_or_text_refused(tmp_path, extra):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, **extra)
    assert result["exit_code"] == 1
    assert not evidence_sidecar_path(plan).exists()


def test_derived_em_row_discharges_and_leaves_plan_bytes(tmp_path):
    repo, plan = _derived_plan(tmp_path)
    before = plan.read_bytes()
    result = _call(repo, plan, id="B1")
    assert result["exit_code"] == 0 and result["applied"] is True, result
    assert plan.read_bytes() == before
    assert set(read_em_discharges(plan)) == {"B1"}
    again = _call(repo, plan, id="B1", text="other")
    assert again["exit_code"] == 0 and again["applied"] is False


def test_writes_row_with_same_body_stays_agent(tmp_path):
    repo, plan = _derived_plan(tmp_path)
    result = _call(repo, plan, id="B2")
    assert result["exit_code"] == 1
    assert "is not performer: em" in str(result)
    assert not evidence_sidecar_path(plan).exists()


def test_derived_em_operator_row_hits_operator_refusal(tmp_path):
    repo, plan = _derived_plan(tmp_path)
    result = _call(repo, plan, id="B3")
    assert result["exit_code"] == 1
    assert "execution_mode: operator" in str(result)
    assert not evidence_sidecar_path(plan).exists()


def test_reader_never_raises(tmp_path):
    plan = tmp_path / "p.md"
    assert read_em_discharges(plan) == {}
    evidence_sidecar_path(plan).write_text(": : [garbled", encoding="utf-8")
    assert read_em_discharges(plan) == {}
    evidence_sidecar_path(plan).write_text("em_discharged: [1, 2]\n", encoding="utf-8")
    assert read_em_discharges(plan) == {}


_ONLY_EM_ROW = """\
---
title: "Discharge Only"
status: ready
---

## Tasks

```yaml plan-tasks
- id: E1
  title: EM step
  change_kind: verification
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: open
  body: |
    Do it.
- id: X1
  title: Executor step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  disposition: open
  body: |
    Code.
```
"""


def test_discharged_row_no_longer_counts_as_open_for_stamp_and_gate(tmp_path):
    """em-discharge leaves disposition open by contract; the close oracles must read the sidecar."""
    from coordinator_core.archive_stamp import _open_spine_row_ids
    from coordinator_core.workstream_complete.directives_spine_worklist import (
        compute_open_spine_row_gate,
    )

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d2.md", "")
    plan.write_text(_ONLY_EM_ROW, encoding="utf-8", newline="")

    assert _open_spine_row_ids(plan) == ["E1", "X1"]
    assert _call(repo, plan)["applied"] is True
    assert _call(repo, plan)["applied"] is False  # the "already discharged" reply

    assert _open_spine_row_ids(plan) == ["X1"]
    gate = compute_open_spine_row_gate(plan.stem, plan)
    assert [r.id for r in gate.rows] == ["X1"]
