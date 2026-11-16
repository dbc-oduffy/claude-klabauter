"""A revise note citing CLAUDE.md or AGENTS.md in passing adds no footprint row; an explicit
"widen to <path>" still widens, and an edit-verb clause still widens an ordinary file."""

from __future__ import annotations

import json

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-02-x-6fcf46-20261102T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-02-p.md"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"
CITATION = "If the corpus is unreachable again, CLAUDE.md sanctions reading engine source directly"

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


def _make(tmp_path, *, halted):
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, OUTSIDE, "CLAUDE.md", "AGENTS.md", "sub/AGENTS.md"):
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
    if halted:
        (rl.run_dir(tmp_path, RUN) / "halted.json").write_text(
            json.dumps({"status": halted_run.STATUS_HALTED}), encoding="utf-8"
        )
    return tmp_path


def _spine_ids(repo):
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return [r["id"] for r in yaml.safe_load(located.body)]


def _uncovered(repo, note):
    return rl._note_uncovered_paths(repo, rl.run_dir(repo, RUN), {"sizing_rel": SIZING_REL}, note)


def test_a_passing_citation_of_a_force_loaded_file_widens_nothing(tmp_path):
    repo = _make(tmp_path, halted=True)
    for note in (CITATION, "AGENTS.md says nothing about this", "see sub/AGENTS.md and CLAUDE.md for context"):
        assert _uncovered(repo, note) == []
    reply = rl._handler({"run_id": RUN, "note": CITATION}, repo)
    assert reply["ok"] is True and "footprint_widened" not in reply
    assert _spine_ids(repo) == ["C1"]


def test_a_passing_citation_of_an_ordinary_file_on_a_halted_run_widens_nothing(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, f"{OUTSIDE} already passes, nothing to do") == []


def test_an_edit_verb_clause_still_widens_an_ordinary_file(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, f"Please fix {OUTSIDE}.") == [OUTSIDE]


def test_an_edit_verb_clause_never_widens_a_force_loaded_file(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "Please update CLAUDE.md and AGENTS.md.") == []


def test_widen_to_names_a_force_loaded_file_as_a_write(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "widen to CLAUDE.md") == ["CLAUDE.md"]
    assert _uncovered(repo, "Widen the footprint to sub/AGENTS.md") == ["sub/AGENTS.md"]


def test_widen_to_accepts_a_backslash_spelled_path(tmp_path):
    repo = _make(tmp_path, halted=False)
    assert _uncovered(repo, "widen to pkg\\outside.py") == [OUTSIDE]
