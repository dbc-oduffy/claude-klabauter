"""workflow.revise widens a staged run's footprint through an engine-written row on the sizing's plan spine
(a governed plan, so the row must pass the governed row schema) when a note, add_paths or withhold_paths
names a path the footprint lacks; with no plan the refusal and its route stay."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-02-x-6fcf46-20261102T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-02-p.md"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"
OTHER = "pkg/other.py"

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


def _make(tmp_path, *, plan: bool):
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, OUTSIDE, OTHER):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    sizing = {"schema": "sizing-object", "intent": "x"}
    if plan:
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8")
        sizing["plan"] = PLAN_REL
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    return _make(tmp_path, plan=True)


@pytest.fixture
def bare_repo(tmp_path):
    return _make(tmp_path, plan=False)


def _spine(repo):
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return yaml.safe_load(located.body)


def _call(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo)


def _footprint(repo):
    return rl.run_footprint(repo, rl.run_dir(repo, RUN), SIZING_REL)


def test_note_naming_an_outside_file_writes_a_row_and_is_stored(repo):
    reply = _call(repo, note=f"also fix {OUTSIDE}")
    assert reply["ok"] is True and "note" in reply["applied"]
    assert not [r for r in reply["refused"] if r.get("input") == "note"]
    (row,) = [r for r in _spine(repo) if r["id"] != "C1"]
    assert row["writes"] == [OUTSIDE] and row["disposition"] == "open"
    assert reply["footprint_widened"] == [{"row": row["id"], "paths": [OUTSIDE]}]
    assert [n["note"] for n in rl.read_notes(rl.run_dir(repo, RUN))] == [f"also fix {OUTSIDE}"]
    assert OUTSIDE in _footprint(repo)[0]


def test_add_paths_outside_widens_the_same_way(repo):
    reply = _call(repo, add_paths=[OUTSIDE])
    assert reply["ok"] is True
    (row,) = [r for r in _spine(repo) if r["id"] != "C1"]
    assert row["writes"] == [OUTSIDE]
    assert reply["footprint_widened"][0]["row"] == row["id"]
    assert OUTSIDE in _footprint(repo)[0]


def test_withhold_paths_outside_widens_the_same_way(repo):
    reply = _call(repo, withhold_paths=[{"path": OUTSIDE, "reason": "peer hunks"}])
    assert reply["ok"] is True and "withhold_paths" in reply["applied"]
    (row,) = [r for r in _spine(repo) if r["id"] != "C1"]
    assert row["writes"] == [OUTSIDE]
    assert (rl.run_dir(repo, RUN) / "withhold.jsonl").is_file()


def test_a_covered_note_writes_no_row(repo):
    reply = _call(repo, note=f"also fix {INSIDE}")
    assert reply["ok"] is True and "footprint_widened" not in reply
    assert [r["id"] for r in _spine(repo)] == ["C1"]


def test_two_widenings_mint_distinct_row_ids(repo):
    _call(repo, note=f"also fix {OUTSIDE}")
    _call(repo, note=f"also fix {OTHER}")
    ids = [r["id"] for r in _spine(repo)]
    assert len(ids) == 3 and len(set(ids)) == 3


def test_no_plan_still_refuses_note_with_the_route(bare_repo):
    reply = _call(bare_repo, note=f"also fix {OUTSIDE}")
    (entry,) = [r for r in reply["refused"] if r.get("input") == "note"]
    assert entry["paths"] == [OUTSIDE] and "add-task" in entry["reason"]
    assert rl.read_notes(rl.run_dir(bare_repo, RUN)) == []


def test_no_plan_still_refuses_add_paths_and_withhold(bare_repo):
    assert "new ask" in _call(bare_repo, add_paths=[OUTSIDE])["error"]
    with pytest.raises(rl.LedgerRefused, match="follow-up ask"):
        rl.revise(bare_repo, RUN, withhold_paths=[{"path": OUTSIDE, "reason": "r"}])
