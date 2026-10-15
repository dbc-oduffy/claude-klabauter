"""plan.tasks.mutate resolve: a hand-committed row is coded exactly as the terminal commit's coded-stamp leaves it."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import resolve_plan_edge
from coordinator_core.ops.dispatch_emit.terminal_commit import _flip_rows_coded
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Hand Commit Plan"
status: draft
schema_version: '1.2.0'
---

# Hand Commit Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  writes:
  - a.py
  queue_scope: project
  deferred: false
  body: |
    First.
```
"""

_GATED = """\
---
title: "Gated Plan"
status: draft
schema_version: '1.2.0'
---

# Gated Plan

## Tasks

```yaml plan-tasks
- id: G1
  title: Gated chunk
  change_kind: script-edit
  surface: g.py
  writes:
  - g.py
  depends_on_plan:
  - {plan: docs/plans/hc.md, chunk: C1}
  queue_scope: project
  deferred: false
  body: |
    Gated.
```
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _hand_commit(repo) -> str:
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "hand commit C1")
    return _git(repo, "rev-parse", "HEAD")


def _resolve(repo, plan, sha, detail="landed by hand"):
    return asyncio.run(_handler(
        {
            "verb": "resolve", "plan_path": str(plan), "id": "C1",
            "disposition": "coded", "disposition_ref": sha, "disposition_detail": detail,
        },
        repo_root=repo / ".git",
    ))


def test_resolve_coded_clears_a_sibling_plans_depends_on_plan_edge(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "hc.md", _PLAN)
    _seed_plan(repo, "gated.md", _GATED)
    edge = {"plan": "docs/plans/hc.md", "chunk": "C1"}

    before = resolve_plan_edge("G1", edge, repo, {})
    assert before.hold is not None

    sha = _hand_commit(repo)
    reply = _resolve(repo, plan, sha)
    assert reply["exit_code"] == 0, reply

    after = resolve_plan_edge("G1", edge, repo, {})
    assert after.hold is None
    assert after.named_row["disposition_ref"] == sha


def test_resolve_coded_leaves_the_row_the_terminal_commit_stamp_leaves(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "hc.md", _PLAN)
    original = plan.read_text(encoding="utf-8")
    sha = _hand_commit(repo)

    reply = _resolve(repo, plan, sha, detail="landed in-run")
    assert reply["exit_code"] == 0, reply
    by_hand = load_rows(plan.read_text(encoding="utf-8")).rows[0]

    stamped_text, flipped = _flip_rows_coded(original, {"C1"}, sha, {"C1": "landed in-run"})
    assert flipped == ["C1"]
    in_run = load_rows(stamped_text).rows[0]

    assert by_hand == in_run
    assert by_hand["disposition"] == "coded"
    assert by_hand["disposition_ref"] == sha
