"""Tests for the plan.tasks.mutate ``clear-gate`` verb.

Pins: a gate with no ``cleared`` key becomes ``cleared: true`` + ``closure_evidence``
and the row then reads as dispatchable; empty evidence refuses with the plan bytes
unchanged; a second call is ``applied: False``; ambiguous and absent matches refuse.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ROW_GATED = """\
- id: C1
  title: Gated chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  external_gate:
    - owner_repo: coordinator-content-repo
      condition: DoE lands the flip
      requires: commit-in-owner-repo
      blocks: execution
  body: |
    Do the thing.
"""

_PLAN = """\
---
title: "Gate Plan"
status: draft
---

# Gate Plan

## Tasks

```yaml plan-tasks
{rows}```

## Trailer
"""

_FM_PLAN = """\
---
title: "Gate Plan"
status: draft
external_gate:
  - id: g1
    row: C1
    owner_repo: coordinator-content-repo
    condition: DoE lands the flip
    blocks: execution
    cleared: false
---

# Gate Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: Gated chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the thing.
```

## Trailer
"""


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {"verb": "clear-gate", "plan_path": str(plan), "id": "C1",
              "owner_repo": "coordinator-content-repo", "evidence": "DoE commit abc1234 landed the flip"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def _excluded(plan: Path) -> list:
    exclusions: list = []
    read_spine(plan, exclusions)
    return [e["id"] for e in exclusions]


def test_row_level_gate_clears_and_row_is_dispatchable(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    assert _excluded(plan) == ["C1"]

    result = _call(repo, plan)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    text = plan.read_text(encoding="utf-8")
    assert "cleared: true" in text
    assert "closure_evidence: DoE commit abc1234 landed the flip" in text
    assert _excluded(plan) == []


def test_empty_evidence_refuses_bytes_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    before = plan.read_bytes()

    for ev in ("", "   "):
        result = _call(repo, plan, evidence=ev)
        assert result["exit_code"] == 1, result
        assert result["applied"] is False
    assert plan.read_bytes() == before


def test_second_call_is_not_applied(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    assert _call(repo, plan)["applied"] is True
    after_first = plan.read_bytes()

    again = _call(repo, plan)

    assert again["exit_code"] == 0, again
    assert again["applied"] is False
    assert plan.read_bytes() == after_first


def test_unknown_row_and_no_matching_gate_refuse(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    before = plan.read_bytes()

    assert _call(repo, plan, id="C9")["exit_code"] == 1
    miss = _call(repo, plan, owner_repo="other-repo")
    assert miss["exit_code"] == 1 and "no external_gate matches" in miss["error"]
    assert plan.read_bytes() == before


def test_two_gates_one_owner_repo_refuse_naming_them(tmp_path):
    repo = _make_git_repo(tmp_path)
    rows = _ROW_GATED.replace(
        "      blocks: execution\n",
        "      blocks: execution\n    - owner_repo: coordinator-content-repo\n      condition: second\n"
        "      blocks: execution\n",
    )
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=rows))
    before = plan.read_bytes()

    result = _call(repo, plan)

    assert result["exit_code"] == 1
    assert "2 external_gate entries match" in result["error"]
    assert plan.read_bytes() == before


def test_frontmatter_gate_with_row_clears(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "fm.md", _FM_PLAN)
    assert _excluded(plan) == ["C1"]

    result = _call(repo, plan)

    assert result["exit_code"] == 0 and result["applied"] is True, result
    text = plan.read_text(encoding="utf-8")
    assert "cleared: true" in text and "cleared: false" not in text
    assert text.count("## Trailer") == 1 and "title: Gate Plan" in text.replace('"', "")
    assert _excluded(plan) == []
    assert _call(repo, plan)["applied"] is False


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _resolve(repo: Path, plan: Path, task_id: str, **extra) -> dict:
    params = {"verb": "resolve", "plan_path": str(plan), "id": task_id, "disposition": "coded"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def _gated_row(task_id: str) -> str:
    return _ROW_GATED.replace("id: C1", f"id: {task_id}")


def test_coded_resolve_clears_own_gate_so_close_out_accepts_ref(tmp_path):
    from coordinator_core.execute_plan_assemble.close_out_and_stamp import (
        DISPOSITION_REF_GATED,
        _disposition_ref_evidence,
    )

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_gated_row("C1") + _gated_row("C2")))
    sha = _head(repo)

    result = _resolve(repo, plan, "C1", disposition_ref=sha)

    assert result["exit_code"] == 0, result
    rows = yaml.safe_load(plan.read_text(encoding="utf-8").split("```yaml plan-tasks\n")[1].split("```")[0])
    by_id = {r["id"]: r for r in rows}
    gate = by_id["C1"]["external_gate"][0]
    assert gate["cleared"] is True and sha in gate["closure_evidence"]
    assert "cleared" not in by_id["C2"]["external_gate"][0]
    verified, rejections = _disposition_ref_evidence(rows, repo)
    assert verified == {"C1"} and DISPOSITION_REF_GATED not in rejections.values()


def test_coded_resolve_with_unverifiable_ref_leaves_gate_open(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_gated_row("C1")))

    result = _resolve(repo, plan, "C1", disposition_ref="deadbeefdeadbeef")

    assert result["exit_code"] == 0, result
    assert "cleared" not in plan.read_text(encoding="utf-8").split("## Trailer")[0].split("external_gate:")[1]


_ROW_STRING_GATE = """\
- id: {id}
  title: Gated chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  external_gate:
    - DoE-must-land-the-flip
  body: |
    Do the thing.
"""

_ROW_PLAIN = """\
- id: {id}
  title: Plain chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the thing.
"""

_FM_MULTI_PLAN = """\
---
title: "Gate Plan"
status: draft
external_gate:
  - id: own
    row: C1
    owner_repo: coordinator-content-repo
    condition: own row gate
    blocks: execution
  - id: other
    row: C2
    owner_repo: coordinator-content-repo
    condition: other row gate
    blocks: execution
  - id: wide
    owner_repo: coordinator-content-repo
    condition: plan wide gate
    blocks: execution
---

# Gate Plan

## Tasks

```yaml plan-tasks
{rows}```

## Trailer
"""


def _fm_gates(plan: Path) -> dict:
    fm = yaml.safe_load(plan.read_text(encoding="utf-8").split("---")[1])
    return {g["id"]: g for g in fm["external_gate"]}


def _uncleared(plan: Path) -> list:
    """Ids of rows `_has_uncleared_execution_gate` still reads as gated (disposition ignored)."""
    from coordinator_core.ops.dispatch_emit.spine_read import (
        _frontmatter_external_gates,
        _has_uncleared_execution_gate,
        load_rows_memo,
    )

    text = plan.read_text(encoding="utf-8")
    rows = [r for r in load_rows_memo(text).rows if isinstance(r, dict)]
    fm = _frontmatter_external_gates(text, {r["id"] for r in rows})
    return [r["id"] for r in rows if _has_uncleared_execution_gate(r, tuple(fm.get(r["id"], ())))]


def test_coded_resolve_clears_own_string_form_gate(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(
        rows=_ROW_STRING_GATE.format(id="C1") + _ROW_STRING_GATE.format(id="C2")))
    assert _uncleared(plan) == ["C1", "C2"]
    sha = _head(repo)

    result = _resolve(repo, plan, "C1", disposition_ref=sha)

    assert result["exit_code"] == 0, result
    assert _uncleared(plan) == ["C2"]


def test_coded_resolve_clears_own_frontmatter_gate_only(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _FM_MULTI_PLAN.format(
        rows=_ROW_PLAIN.format(id="C1") + _ROW_PLAIN.format(id="C2")))
    assert _uncleared(plan) == ["C1", "C2"]
    sha = _head(repo)

    result = _resolve(repo, plan, "C1", disposition_ref=sha)

    assert result["exit_code"] == 0, result
    gates = _fm_gates(plan)
    assert gates["own"]["cleared"] is True and sha in gates["own"]["closure_evidence"]
    assert "cleared" not in gates["other"] and "cleared" not in gates["wide"]
    assert _uncleared(plan) == ["C2"]


def test_non_coded_resolve_leaves_gates_open(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _FM_MULTI_PLAN.format(
        rows=_gated_row("C1") + _ROW_PLAIN.format(id="C2")))
    before = _fm_gates(plan)

    result = _resolve(repo, plan, "C1", disposition="voided", reason="no longer needed")

    assert result["exit_code"] == 0, result
    assert _fm_gates(plan) == before
    assert "cleared" not in plan.read_text(encoding="utf-8").split("## Tasks")[1]
    assert _uncleared(plan) == ["C1", "C2"]


def test_coded_resolve_with_unverifiable_ref_leaves_frontmatter_gate_open(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _FM_MULTI_PLAN.format(
        rows=_ROW_PLAIN.format(id="C1") + _ROW_PLAIN.format(id="C2")))
    before = _fm_gates(plan)

    _resolve(repo, plan, "C1", disposition_ref="0" * 40)

    assert _fm_gates(plan) == before
    assert _uncleared(plan) == ["C1", "C2"]


def test_verb_list_error_names_clear_gate(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = asyncio.run(_handler({"plan_path": "x"}, repo_root=repo / ".git"))
    assert "clear-gate" in result["error"]
