"""A fresh-refire sizing carries its predecessor's family; ask_stage refuses a sizing that lacks a family its predecessor or plan row names."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.session_baton import store

SID = "sid-family-carry"
PRIOR_RUN = "warp-2026-11-14-pred-1-1a-codex-20261118T004733-d43b3c"
PRIOR_SIZING = "state/sizings/2026-11/2026-11-14-pred-1-1a-codex.yaml"
PLAN_REL = "docs/plans/2026-11-01-family-carry-fixture.md"
SIZING_REL = "state/sizings/2026-11-01-xs-carry.yaml"
RUNNER = "coordinator:family-stage-runner"

_PLAN = """---
deliverable_id: dlv-family-abc123
---
# Carry fixture

## Problem

One row.

## Tasks

```yaml plan-tasks
- id: C1
  title: carry row
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  body: |
    {body}
```
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("carry the family", session_id=SID)["ok"]
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "2026-11").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    return tmp_path


def _predecessor(repo: Path, family: str | None = "codex") -> None:
    doc = {"schema": "sizing-object", "name": "pred", "intent": "prior"}
    if family:
        doc["family"] = family
    (repo / PRIOR_SIZING).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    run_dir = repo / "state" / "warp" / PRIOR_RUN
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": PRIOR_SIZING}), encoding="utf-8", newline="\n")


def _mint(repo: Path, **entry) -> dict:
    out = ask_fanout.fanout(
        {"asks": [{"ask": "fresh refire", "source": "relayed", "writes": ["pkg/a.py"], **entry}], "session_id": SID}, repo
    )
    assert out["exit_code"] == 0, out
    return yaml.safe_load((repo / out["ready"][0]["sizing"]).read_text(encoding="utf-8"))


def _stage(repo: Path, body: str = "plain row", **sizing_over):
    (repo / PLAN_REL).write_text(_PLAN.format(body=body), encoding="utf-8", newline="\n")
    sizing = {"intent": "Fix", "exit_criterion": {"statement": "ok"}, "deliverable_id": "dlv-xs-abc123"}
    sizing.update(sizing_over)
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    return stage(repo, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)


def test_mint_carries_the_family_of_the_run_it_supersedes(repo) -> None:
    _predecessor(repo)
    record = _mint(repo, em_context=f"Fresh refire; supersedes halted run {PRIOR_RUN} (old engine).")
    assert record["family"] == "codex"


def test_mint_carries_the_family_of_a_superseded_sizing_path(repo) -> None:
    _predecessor(repo)
    assert _mint(repo, ask=f"Redo this; it supersedes {PRIOR_SIZING}.")["family"] == "codex"


def test_mint_names_no_family_when_the_predecessor_has_none_or_is_only_mentioned(repo) -> None:
    _predecessor(repo, family=None)
    assert "family" not in _mint(repo, em_context=f"supersedes halted run {PRIOR_RUN}")
    _predecessor(repo)
    assert "family" not in _mint(repo, em_context=f"see run {PRIOR_RUN} for background")


def test_stage_refuses_a_sizing_with_no_family_whose_superseded_run_names_one(repo) -> None:
    _predecessor(repo)
    with pytest.raises(AskStageError) as exc:
        _stage(repo, em_analysis={"em_brief": f"supersedes halted run {PRIOR_RUN}"})
    assert "'codex'" in str(exc.value) and PRIOR_RUN in str(exc.value) and "family: codex" in str(exc.value)


def test_stage_refuses_a_sizing_with_no_family_whose_plan_row_names_one(repo) -> None:
    with pytest.raises(AskStageError) as exc:
        _stage(repo, body="this run's sizing family: codex routes this row to the runner")
    assert "'codex'" in str(exc.value) and "plan row" in str(exc.value)


def test_stage_still_accepts_the_sizing_that_names_the_family(repo) -> None:
    _predecessor(repo)
    m = _stage(repo, body="sizing family: codex", family="codex", em_analysis={"em_brief": f"supersedes run {PRIOR_RUN}"})
    assert m.rows[0].agent_type == RUNNER


def test_stage_accepts_a_family_free_sizing_with_no_predecessor_or_row_family(repo) -> None:
    _predecessor(repo)
    m = _stage(repo, em_analysis={"em_brief": f"see run {PRIOR_RUN} for background"})
    assert m.rows[0].agent_type == "coordinator:executor"
