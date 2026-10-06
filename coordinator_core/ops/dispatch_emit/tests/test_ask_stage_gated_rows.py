"""ask_stage stages ungated rows and lists gated rows in the manifest instead of refusing the run."""

from __future__ import annotations

import time

from coordinator_core.ops.dispatch_emit.ask_stage import _handler

PLAN_REL = "docs/plans/2026-10-03-gated.md"


def _plan(gate_block: str, extra_dep: bool = False) -> str:
    dep = "  depends_on:\n    - chunk: G1\n" if extra_dep else ""
    default_dep = "  depends_on:\n    - chunk: U1\n"
    return f"""---
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
- id: G1
  title: gated
  change_kind: code-edit
  surface: pkg/g.py
  writes:
    - pkg/g.py
{gate_block}
- id: T1
  title: dependent
  change_kind: code-edit
  surface: pkg/t.py
  writes:
    - pkg/t.py
{dep or default_dep}```
"""


def _stage(tmp_path, plan: str):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return _handler({"run_id": "r1", "plan_path": PLAN_REL}, repo_root=tmp_path)


def _gate(requires: str, blocks: str | None = None) -> str:
    b = f"      blocks: {blocks}\n" if blocks else ""
    return f"  external_gate:\n    - owner_repo: other\n      requires: {requires}\n{b}".rstrip("\n")


def test_landed_work_gate_withholds_row_into_manifest(tmp_path):
    start = time.process_time()
    reply = _stage(tmp_path, _plan(_gate("landed-work")))
    assert (time.process_time() - start) * 1000 < 500
    assert "error" not in reply
    assert [r["id"] for r in reply["rows"]] == ["U1", "T1"]
    assert [(g["id"], g["reason"]) for g in reply["gated"]] == [("G1", "external_gate")]


def test_commit_in_owner_repo_gate_behaves_identically(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("commit-in-owner-repo")))
    assert [r["id"] for r in reply["rows"]] == ["U1", "T1"]
    assert [g["id"] for g in reply["gated"]] == ["G1"]


def test_ac_closure_gate_is_staged_as_an_ordinary_row(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("landed-work", "ac-closure")))
    assert "G1" in [r["id"] for r in reply["rows"]]
    assert reply["gated"] == []


def test_dependent_of_gated_row_is_gated_transitively(tmp_path):
    reply = _stage(tmp_path, _plan(_gate("landed-work"), extra_dep=True))
    assert [r["id"] for r in reply["rows"]] == ["U1"]
    assert [(g["id"], g["reason"]) for g in reply["gated"]] == [
        ("G1", "external_gate"),
        ("T1", "transitive_gate_closure"),
    ]
