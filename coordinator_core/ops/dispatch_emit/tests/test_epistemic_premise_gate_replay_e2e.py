"""Replay of the 46693e C1/C2 epistemic-premise shape through the real ask_stage entry at default arguments.

A staged manifest row is one executor spawn; the verdict is recorded through the real premise-record verb.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.premise_gate import PREMISE_RECORD_BRIEF_LINE
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.plan_tasks_mutate import _handler as mutate_handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"
_NOTE = "runs only if C1 records overlap yes; else the row closes wont_do citing C1"

_ROWS = """\
- id: C1
  title: overlap probe
  change_kind: doc-edit
  surface: docs/x/c1.md
  writes:
    - docs/x/c1.md
{c1_disposition}- id: C2
  title: fold
  change_kind: doc-edit
  surface: docs/x/c2.md
  writes:
    - docs/x/c2.md
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: "{note}"
- id: D3
  title: consumer
  change_kind: doc-edit
  surface: docs/x/d3.md
  writes:
    - docs/x/d3.md
  depends_on:
    - chunk: C2
      gate_kind: output-consumption-runtime
"""


def _plan(tmp_path: Path, *, created: str = "2026-10-27", c1_done: bool = True) -> tuple[Path, Path]:
    repo = _make_git_repo(tmp_path)
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    done = "  disposition: coded\n  disposition_ref: abc1234\n" if c1_done else ""
    plan.write_text(
        f"---\ntitle: p\ncreated: {created}\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n"
        f"{_ROWS.format(c1_disposition=done, note=_NOTE)}```\n",
        encoding="utf-8",
        newline="\n",
    )
    return repo, plan


def _record(repo: Path, plan: Path, verdict: str) -> None:
    result = asyncio.run(
        mutate_handler(
            {"verb": "premise-record", "plan_path": str(plan), "id": "C1", "verdict": verdict, "ref": "docs/x/c1.md"},
            repo_root=repo / ".git",
        )
    )
    assert result["exit_code"] == 0, result


def _stage(repo: Path, run_id: str = "r1") -> dict:
    """Default-argument ask_stage; {} when nothing is dispatchable (the stage refuses a zero-row spine)."""
    reply = read_stage_reply(repo, _handler({"run_id": run_id, "plan_path": PLAN_REL}, repo_root=repo))
    if "error" in reply:
        assert "zero dispatchable rows" in reply["error"], reply
        return {}
    return reply


def _spawned(reply: dict) -> list[str]:
    return [r["id"] for r in reply.get("rows", ())]


def _rows(plan: Path) -> dict:
    body = plan.read_text(encoding="utf-8").split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r for r in yaml.safe_load(body)}


@pytest.mark.parametrize("record", ["no", None])
def test_premise_no_or_absent_closes_c2_wont_do_and_spawns_neither_c2_nor_d3(tmp_path, record):
    repo, plan = _plan(tmp_path)
    if record:
        _record(repo, plan, record)
    spawned = _spawned(_stage(repo))
    assert "C2" not in spawned and "D3" not in spawned
    c2 = _rows(plan)["C2"]
    assert c2["disposition"] == "wont_do"
    assert c2["premise_close"]["chunk"] == "C1"
    assert c2["premise_close"]["verdict"] == (record or "absent")


def test_premise_yes_dispatches_c2_and_d3(tmp_path):
    repo, plan = _plan(tmp_path)
    _record(repo, plan, "yes")
    spawned = _spawned(_stage(repo))
    assert spawned.count("C2") == 1 and spawned.count("D3") == 1
    assert "disposition" not in _rows(plan)["C2"]


def test_legacy_plan_with_no_record_keeps_sequencing_only(tmp_path):
    repo, plan = _plan(tmp_path, created="2026-09-01")
    spawned = _spawned(_stage(repo))
    assert spawned.count("C2") == 1 and spawned.count("D3") == 1
    assert "disposition" not in _rows(plan)["C2"]


def test_premise_recorded_after_a_first_stage_closes_c2_at_the_next_stage(tmp_path):
    # The 46693e repro as far as the engine reaches: stage with C1 open, C1 records no, the next stage closes C2.
    repo, plan = _plan(tmp_path, c1_done=False)
    first = _spawned(_stage(repo, "r1"))
    assert {"C1", "C2", "D3"} <= set(first)
    plan_text = plan.read_text(encoding="utf-8").replace(
        "  surface: docs/x/c1.md\n", "  surface: docs/x/c1.md\n  disposition: coded\n  disposition_ref: abc1234\n", 1
    )
    plan.write_text(plan_text, encoding="utf-8", newline="\n")
    _record(repo, plan, "no")
    second = _spawned(_stage(repo, "r2"))
    assert "C2" not in second and "D3" not in second
    assert _rows(plan)["C2"]["disposition"] == "wont_do"


def test_the_brief_composed_for_the_premise_row_carries_the_record_line(tmp_path):
    repo, _ = _plan(tmp_path, c1_done=False)
    reply = _stage(repo)
    briefs = {r["id"]: (repo / r["brief_path"]).read_text(encoding="utf-8") for r in reply["rows"]}
    assert PREMISE_RECORD_BRIEF_LINE in briefs["C1"]
    assert PREMISE_RECORD_BRIEF_LINE not in briefs["C2"]
