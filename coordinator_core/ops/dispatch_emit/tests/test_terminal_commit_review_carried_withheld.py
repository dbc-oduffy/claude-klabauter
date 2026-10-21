"""A review-only marker's ``review_carried`` paths are withheld from dispatch.terminal_commit's
landing and reported under the carried reason."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_remedy, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixtures
    SID,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = """---
title: p
status: executing
plan_id: pln-x-123456
---

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def _fire(repo, carried):
    (repo / "plan.md").write_text(_PLAN, encoding="utf-8", newline="")
    base = _git(["rev-parse", "HEAD"], repo)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py", "b.py")),),
        plan_path="plan.md", review_only=True, review_open_rows=("C3",),
        review_run_base=base, review_carried=carried,
    )
    (repo / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(repo, "run-1", "run.mjs")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [],
         "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0},
         "session_id": SID, "run_id": "run-1", "run_token": token},
        repo_root=repo / ".git",
    )


def test_a_carried_path_is_withheld_and_never_staged(repo):
    (repo / "a.py").write_text("A\n", encoding="utf-8", newline="")
    (repo / "b.py").write_text("B\n", encoding="utf-8", newline="")
    out = _fire(repo, ("a.py",))
    assert out.get("committed") is True, out
    tracked = _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()
    assert "a.py" not in tracked
    assert "b.py" in tracked
    assert landing_remedy.REVIEW_CARRIED_REASON in str(out)
    assert landing_remedy.REVIEW_CARRIED_REASON in out["run_artifacts"]["withheld"]["a.py"]


def test_without_the_carried_field_the_path_lands(repo):
    (repo / "a.py").write_text("A\n", encoding="utf-8", newline="")
    (repo / "b.py").write_text("B\n", encoding="utf-8", newline="")
    out = _fire(repo, ())
    assert out.get("committed") is True, out
    assert "a.py" in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()
