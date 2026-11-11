"""A revise note's bare punctuation token ("-", "--", "...") never becomes a write path, even when a
repo file of that name exists; a real repo path in the same note still widens the footprint."""

from __future__ import annotations

import json

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-02-x-6fcf46-20261102T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-02-p.md"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"

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
    for rel in (INSIDE, OUTSIDE, "-", "--"):
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


def _spine_ids(repo):
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return [r["id"] for r in yaml.safe_load(located.body)]


def test_punctuation_tokens_are_never_paths_even_when_the_file_exists(tmp_path):
    repo = _make(tmp_path)
    note = "Halted. Remaining: (1) update the assertions - and ... then -- refire"
    assert rl._paths_named(repo, [note]) == []
    reply = rl._handler({"run_id": RUN, "note": note}, repo)
    assert reply["ok"] is True and "footprint_widened" not in reply
    assert _spine_ids(repo) == ["C1"]


def test_a_real_repo_path_in_the_same_note_still_widens(tmp_path):
    repo = _make(tmp_path)
    note = f"Remaining: - fix {OUTSIDE} ..."
    assert rl._paths_named(repo, [note]) == [OUTSIDE]
    reply = rl._handler({"run_id": RUN, "note": note}, repo)
    assert reply["footprint_widened"][0]["paths"] == [OUTSIDE]
    assert len(_spine_ids(repo)) == 2
