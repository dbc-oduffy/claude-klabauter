"""A synthesis that confirms its downstream rows and widens scope never closes them premise-false.

Fixture shape: the `## Premise verdict for the downstream rows` section of
docs/research/terminal-commit-real-shape/2026-11-14-phase-attribution/synthesis.md (two legs
confirmed over budget, a third leg the plan lacks). The verdict is read from the structured
`premises` sidecar key, never from the synthesis prose.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.plan_tasks_mutate import _handler as mutate_handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"
SYNTHESIS_REL = "docs/research/x/synthesis.md"
ADDED = "peer_hunk_gate.landing_holds is the third over-budget leg and has no plan row"

_SYNTHESIS = """# Synthesis

## Premise verdict for the downstream rows

- `_checkin_bytes` (C2a): over budget (1.8-4.8s). The marker names it correctly.
- `_pre_commit_gates` (C2b): 1.66s in a completed landing, over budget.
- NOT in the plan's rows: `peer_hunk_gate.landing_holds` (about 11s). The EM must route this leg.
"""

_PLAN = """---
title: p
created: 2026-10-27
---

# p

## Tasks

```yaml plan-tasks
- id: C1
  title: attribute
  change_kind: doc-edit
  surface: docs/research/x/synthesis.md
  writes:
    - docs/research/x/synthesis.md
  disposition: coded
  disposition_ref: abc1234
- id: C2a
  title: cut checkin
  change_kind: doc-edit
  surface: docs/x/c2a.md
  writes:
    - docs/x/c2a.md
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
- id: C2b
  title: cut gates
  change_kind: doc-edit
  surface: docs/x/c2b.md
  writes:
    - docs/x/c2b.md
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
```
"""


def _repo(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(_PLAN, encoding="utf-8", newline="\n")
    synthesis = repo / SYNTHESIS_REL
    synthesis.parent.mkdir(parents=True, exist_ok=True)
    synthesis.write_text(_SYNTHESIS, encoding="utf-8", newline="\n")
    return repo, plan


def _record(repo, plan, verdict, **extra):
    params = {
        "verb": "premise-record", "plan_path": str(plan), "id": "C1", "verdict": verdict, "ref": SYNTHESIS_REL,
        **extra,
    }
    result = asyncio.run(mutate_handler(params, repo_root=repo / ".git"))
    assert result["exit_code"] == 0, result


def _stage(repo) -> dict:
    return read_stage_reply(repo, _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=repo))


def test_widened_verdict_halts_for_the_em_naming_the_scope_and_never_closes(tmp_path):
    repo, plan = _repo(tmp_path)
    _record(repo, plan, "widened", added_scope=ADDED)
    err = _stage(repo)["error"]
    assert err.startswith("PREMISE-WIDENED")
    assert "C2a" in err and "C2b" in err and ADDED in err
    assert "premise-false" not in err and "zero dispatchable rows" not in err


def test_widened_without_added_scope_is_refused_at_record_time(tmp_path):
    repo, plan = _repo(tmp_path)
    params = {
        "verb": "premise-record", "plan_path": str(plan), "id": "C1", "verdict": "widened", "ref": SYNTHESIS_REL,
    }
    result = asyncio.run(mutate_handler(params, repo_root=repo / ".git"))
    assert result["exit_code"] == 1 and "added_scope" in result["error"]


def test_no_still_closes_premise_false(tmp_path):
    repo, plan = _repo(tmp_path)
    _record(repo, plan, "no")
    err = _stage(repo)["error"]
    assert "zero dispatchable rows" in err
    assert "C2a (premise-false" in err and "C2b (premise-false" in err
    assert "PREMISE-WIDENED" not in err


def test_widened_then_amended_and_rerecorded_yes_dispatches_both_rows(tmp_path):
    repo, plan = _repo(tmp_path)
    _record(repo, plan, "widened", added_scope=ADDED)
    assert "PREMISE-WIDENED" in _stage(repo)["error"]
    _record(repo, plan, "yes")
    reply = _stage(repo)
    assert "error" not in reply, reply
    assert {r["id"] for r in reply["rows"]} == {"C2a", "C2b"}
