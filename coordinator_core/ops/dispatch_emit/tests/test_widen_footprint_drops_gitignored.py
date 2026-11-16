"""workflow.revise's footprint-cover row never lists a gitignored, untracked path, so a later
`commit_v2._ignored_untracked` checkpoint has nothing to refuse."""

from __future__ import annotations

import json

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-02-x-6fcf46-20261102T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-02-p.md"
INSIDE = "pkg/inside.py"
NORMAL = "pkg/outside.py"
IGNORED = "scratch/c4n17-fbx-diff/diff.py"

_PLAN = """---
title: p
created: 2026-11-02
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
- id: C1
  title: first
  writes:
    - pkg/inside.py
```
"""


def _make(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    for rel in (INSIDE, NORMAL, IGNORED):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(
        yaml.safe_dump({"schema": "sizing-object", "intent": "x", "plan": PLAN_REL}), encoding="utf-8"
    )
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _added_rows(repo):
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return [r for r in yaml.safe_load(located.body) if r["id"] != "C1"]


def test_cover_row_omits_the_ignored_path_and_keeps_the_normal_one(tmp_path) -> None:
    repo = _make(tmp_path)
    reply = rl._handler({"run_id": RUN, "add_paths": [IGNORED, NORMAL]}, repo)
    (row,) = _added_rows(repo)
    assert row["writes"] == [NORMAL]
    assert reply["footprint_widened"] == [{"row": row["id"], "paths": [NORMAL]}]
    assert reply["footprint_ignored_dropped"] == [IGNORED]
    covered = row["writes"]
    assert commit_v2._ignored_untracked(repo, covered, head_absent=set(covered)) == []
    assert commit_v2._ignored_untracked(repo, [IGNORED], head_absent={IGNORED}) == [IGNORED]


def test_all_ignored_writes_no_row(tmp_path) -> None:
    repo = _make(tmp_path)
    reply = rl._handler({"run_id": RUN, "add_paths": [IGNORED]}, repo)
    assert _added_rows(repo) == []
    assert "footprint_widened" not in reply
    assert reply["footprint_ignored_dropped"] == [IGNORED]


def test_non_ignored_paths_are_still_covered(tmp_path) -> None:
    repo = _make(tmp_path)
    rl._handler({"run_id": RUN, "add_paths": [NORMAL]}, repo)
    (row,) = _added_rows(repo)
    assert row["writes"] == [NORMAL]
