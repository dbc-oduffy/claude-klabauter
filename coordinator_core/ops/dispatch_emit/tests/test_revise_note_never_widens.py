"""workflow.revise: a free-text note never widens a run's footprint (a "do not edit X" note included);
`add_paths` and a PM verbatim naming the path still do."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-26-x-bc2a80-20261126T080433-13c9d2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-26-p.md"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"

_PLAN = """---
title: p
created: 2026-11-26
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


def _sizing() -> dict:
    return {
        "schema": "sizing-object", "intent": "x", "plan": PLAN_REL,
        "estimate": {"tshirt": "S", "provisional": True}, "route": "dispatch", "detents": [], "fork": None,
        "xl_exit": None, "status": "sized", "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    }


def _make(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, OUTSIDE):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(_sizing()), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _spine_ids(repo: Path) -> list:
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return [r["id"] for r in yaml.safe_load(located.body)]


def test_do_not_edit_note_leaves_the_footprint_unchanged(tmp_path: Path) -> None:
    repo = _make(tmp_path)
    reply = rl._handler({"run_id": RUN, "note": f"do not edit {OUTSIDE}"}, repo)
    assert reply["ok"] is True and "footprint_widened" not in reply
    assert _spine_ids(repo) == ["C1"]
    assert rl.paths_outside_footprint(repo, rl.run_dir(repo, RUN), SIZING_REL, [OUTSIDE]) == [OUTSIDE]


def test_edit_verb_and_widen_to_notes_widen_nothing(tmp_path: Path) -> None:
    repo = _make(tmp_path)
    for note in (f"please fix {OUTSIDE}", f"widen to {OUTSIDE}"):
        reply = rl._handler({"run_id": RUN, "note": note}, repo)
        assert reply["ok"] is True and "footprint_widened" not in reply
    assert _spine_ids(repo) == ["C1"]


def test_add_paths_widens_the_footprint(tmp_path: Path) -> None:
    repo = _make(tmp_path)
    reply = rl._handler({"run_id": RUN, "add_paths": [OUTSIDE]}, repo)
    assert reply["footprint_widened"][0]["paths"] == [OUTSIDE]
    assert len(_spine_ids(repo)) == 2


def test_add_paths_widens_beside_a_do_not_edit_note_for_another_path(tmp_path: Path) -> None:
    repo = _make(tmp_path)
    other = "pkg/other.py"
    (repo / other).write_text("x = 1\n", encoding="utf-8")
    reply = rl._handler({"run_id": RUN, "add_paths": [OUTSIDE], "note": f"do not edit {other}"}, repo)
    assert [p for w in reply["footprint_widened"] for p in w["paths"]] == [OUTSIDE]


def test_a_verbatim_requirement_naming_a_path_still_widens(tmp_path: Path) -> None:
    repo = _make(tmp_path)
    reply = rl._handler({"run_id": RUN, "verbatim": f"please fix {OUTSIDE}"}, repo)
    assert reply["footprint_widened"][0]["paths"] == [OUTSIDE]


def test_a_verbatim_naming_a_path_no_plan_row_can_cover_is_refused_and_not_stored(tmp_path: Path) -> None:
    repo = _make(tmp_path)
    (repo / SIZING_REL).write_text(yaml.safe_dump({"schema": "sizing-object", "intent": "x"}), encoding="utf-8")
    reply = rl._handler({"run_id": RUN, "verbatim": f"please fix {OUTSIDE}"}, repo)
    assert "footprint_widened" not in reply
    refused = [r for r in reply["refused"] if r["input"] == "verbatim"]
    assert refused and refused[0]["paths"] == [OUTSIDE]
    assert "next" not in reply and "not stored" in reply["note"]
    assert _spine_ids(repo) == ["C1"]
