"""A revise note's quoted path resolves against the tracked tree: an exact file stays itself, a unique
suffix match becomes its real repo-relative path, and an ambiguous or unmatched token (or halted
needs_paths entry) is never written as a repo-root path."""

from __future__ import annotations

import json

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests._git_fixture import _git

RUN = "warp-2026-11-23-x-6fcf46-20261123T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-23-p.md"
INSIDE = "pkg/inside.py"
REAL_COLOUR = "tools/likeness-prototype/tests/test_colour_guards.py"
REAL_GEOM = "tools/likeness-prototype/tests/test_geometry_guards.py"
REQ_A = "tools/likeness-prototype/requirements.txt"
REQ_B = "tools/likeness-prototype/requirements-dev.txt"
TWIN_A = "a/tests/test_twin.py"
TWIN_B = "b/tests/test_twin.py"
ROOT_FILE = "tests/test_root_only.py"

_PLAN = """---
title: p
created: 2026-11-23
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


def _make(tmp_path, *, halted, needs=None):
    _git(["init", "-q"], tmp_path)
    for rel in (INSIDE, REAL_COLOUR, REAL_GEOM, REQ_A, "tools/likeness-prototype/requirements-dev.txt",
                "tools/other/requirements.txt", TWIN_A, TWIN_B, ROOT_FILE):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(
        yaml.safe_dump({"schema": "sizing-object", "intent": "x", "plan": PLAN_REL}), encoding="utf-8"
    )
    _git(["add", "."], tmp_path)
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if halted:
        marker = {"status": halted_run.STATUS_HALTED}
        if needs is not None:
            marker["self_correction"] = {"needs_paths": needs}
        (rl.run_dir(tmp_path, RUN) / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    return tmp_path


def _uncovered(repo, note):
    return rl._note_uncovered_paths(repo, rl.run_dir(repo, RUN), {"sizing_rel": SIZING_REL}, note)


def _spine_writes(repo):
    located = locate_fenced_block((repo / PLAN_REL).read_text(encoding="utf-8"))
    assert located.status is LocateStatus.LOCATED
    return [w for row in yaml.safe_load(located.body) for w in row.get("writes", [])]


def test_a_suffix_quoted_path_resolves_to_its_real_repo_relative_file(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "Please fix tests/test_colour_guards.py and tests/test_geometry_guards.py.") == [
        REAL_COLOUR, REAL_GEOM,
    ]


def test_the_default_revise_widen_writes_the_real_path_never_the_root_path(tmp_path):
    repo = _make(tmp_path, halted=True)
    reply = rl._handler({"run_id": RUN, "note": "Please fix tests/test_colour_guards.py."}, repo)
    assert reply["ok"] is True
    writes = _spine_writes(repo)
    assert REAL_COLOUR in writes and "tests/test_colour_guards.py" not in writes


def test_widen_to_resolves_a_suffix_path(tmp_path):
    repo = _make(tmp_path, halted=False)
    assert _uncovered(repo, "widen to tests/test_colour_guards.py") == [REAL_COLOUR]


def test_an_ambiguous_suffix_adds_no_write(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "Please fix tests/test_twin.py and requirements.txt.") == []
    assert _uncovered(repo, "widen to tests/test_twin.py") == []


def test_a_bare_name_with_two_tracked_homes_adds_no_root_write(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "Please update requirements.txt") == []


def test_an_unmatched_path_adds_no_write(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "Please fix tests/test_nowhere.py") == []


def test_an_exact_root_relative_file_still_resolves_to_itself(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, f"Please fix {ROOT_FILE}") == [ROOT_FILE]


def test_halted_needs_paths_resolve_by_suffix_and_drop_the_unresolvable(tmp_path):
    repo = _make(
        tmp_path, halted=True,
        needs=["tests/test_colour_guards.py", "tests/test_twin.py", "requirements-dev.txt", "nowhere/test_x.py",
               "requirements-missing.txt", ".\\tests\\test_root_only.py"],
    )
    assert _uncovered(repo, "Please fix the guards") == [REAL_COLOUR, REQ_B, ROOT_FILE]


def test_a_halted_needs_path_for_a_new_file_in_a_tracked_directory_is_kept(tmp_path):
    repo = _make(tmp_path, halted=True, needs=["tests/test_brand_new.py", "pkg/sub/new.py"])
    assert _uncovered(repo, "Please fix the guards") == ["tests/test_brand_new.py"]


def test_a_new_file_path_is_not_kept_from_a_revise_note_alone(tmp_path):
    repo = _make(tmp_path, halted=True)
    assert _uncovered(repo, "Please fix tests/test_brand_new.py") == []
