"""workflow.revise add_paths gives a path to its cover row alone: no original plan row lists it, and a path
an original row already owns is never moved to a cover row."""

from __future__ import annotations

import json

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-18-x-98cc40-20261118T092315-f7fe3f"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-18-p.md"
INSIDE = "pkg/inside.py"
MOVED = "pkg/moved.py"

_HEAD = """---
title: p
created: 2026-11-18
grouping_approvals:
  do:
    status: pending
  spun_off:
    status: pending
  defer:
    status: pending
  ruled_out:
    status: pending
---

# p

## Tasks

```yaml plan-tasks
"""


def _make(tmp_path, rows: list):
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, MOVED):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_HEAD + yaml.safe_dump(rows, sort_keys=False) + "```\n", encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(
        yaml.safe_dump({"schema": "sizing-object", "intent": "x", "plan": PLAN_REL}), encoding="utf-8"
    )
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _rows(repo) -> dict:
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return {r["id"]: r for r in yaml.safe_load(located.body)}


def test_uncovered_path_goes_to_the_cover_row_and_no_original_row_lists_it(tmp_path) -> None:
    repo = _make(tmp_path, [{"id": "C1", "title": "first", "writes": [INSIDE]}])

    reply = rl._handler({"run_id": RUN, "add_paths": [MOVED]}, repo)

    rows = _rows(repo)
    assert reply["footprint_widened"] == [{"row": "W1", "paths": [MOVED]}]
    assert rows["W1"]["writes"] == [MOVED]
    assert rows["C1"]["writes"] == [INSIDE]


def test_a_path_an_original_row_owns_gets_no_cover_row(tmp_path) -> None:
    repo = _make(tmp_path, [{"id": "C1", "title": "first", "writes": [INSIDE, MOVED]}])

    reply = rl._handler({"run_id": RUN, "add_paths": [MOVED]}, repo)

    assert "footprint_widened" not in reply
    assert set(_rows(repo)) == {"C1"}
