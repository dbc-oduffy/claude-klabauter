"""An --only-incomplete re-emit reviews the halted run's landed rows from that run's base."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

_PLAN = """---
title: "p"
sizing_object: null
---

# p

## Goal

g

## Tasks

```yaml plan-tasks
- id: D0
  title: tD0
  change_kind: doc-edit
  surface: docs/x/d0.md
  writes:
    - docs/x/d0.md
  queue_scope: project
  disposition: open
  body: |
    Do D0.
- id: X0
  title: tX0
  change_kind: doc-edit
  surface: docs/x/x0.md
  writes:
    - docs/x/x0.md
  queue_scope: project
  disposition: open
  body: |
    Do X0.
```
"""


def _git(root, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path, *, trailers):
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    plan = root / "docs" / "plans" / "p.md"
    plan.write_text(_PLAN, encoding="utf-8")
    _git(root, "add", "--", "docs/plans/p.md")
    _git(root, "commit", "-q", "-m", "seed")
    base = _git(root, "rev-parse", "HEAD")
    if trailers:
        (root / "d0.txt").write_text("d0", encoding="utf-8")
        _git(root, "add", "--", "d0.txt")
        _git(
            root, "commit", "-q", "-m", "checkpoint(wave 1): 1 rows — D0", "-m",
            f"Checkpoint-Plan: docs/plans/p.md\nCheckpoint-Base: {base}",
        )
    return root, plan, base


def _emit(root, plan):
    return emit_script(plan, repo_root=root, landed_rows=frozenset(), **REVIEW_KW)


@pytest.mark.spawns_process
def test_reemit_reviews_from_the_halted_runs_base_and_surface(tmp_path):
    root, plan, base = _repo(tmp_path, trailers=True)
    script = _emit(root, plan)

    assert f"Checkpoint-Review-Base: {base}" in script
    assert f"run_base_sha: {base}" in script
    assert f"--range {base}" in script
    assert "run_base_sha: ' + _runBase + '\\ndeclared_paths:" not in script
    # D0 is dropped from the dispatch rows but its path joins the review surface.
    assert "_rows['D0']" not in script and "_rows['X0']" in script
    assert "declared_paths: docs/x/d0.md, docs/x/x0.md" in script


@pytest.mark.spawns_process
def test_a_second_reemit_keeps_the_original_base_from_the_review_trailer(tmp_path):
    root, plan, base = _repo(tmp_path, trailers=True)
    (root / "x0.txt").write_text("x0", encoding="utf-8")
    _git(root, "add", "--", "x0.txt")
    _git(
        root, "commit", "-q", "-m", "checkpoint(wave 1): 1 rows — X0", "-m",
        f"Checkpoint-Plan: docs/plans/p.md\nCheckpoint-Base: {_git(root, 'rev-parse', 'HEAD')}\n"
        f"Checkpoint-Review-Base: {base}",
    )
    from coordinator_core.ops.dispatch_emit.emit import checkpoint_landed_state

    assert checkpoint_landed_state(root, "docs/plans/p.md")[2] == base


@pytest.mark.spawns_process
def test_no_checkpoint_commit_leaves_the_run_base_marker_and_no_review_trailer(tmp_path):
    root, plan, _ = _repo(tmp_path, trailers=False)
    script = _emit(root, plan)

    assert "Checkpoint-Review-Base" not in script
    assert "run_base_sha: ' + _runBase + '\\ndeclared_paths:" in script
    assert "declared_paths: docs/x/d0.md, docs/x/x0.md" in script
    assert "_rows['D0']" in script and "_rows['X0']" in script
