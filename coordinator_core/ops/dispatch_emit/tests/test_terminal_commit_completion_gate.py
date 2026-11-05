"""An ask run whose criterion is not judge-backed commits its work but reports a halted outcome, not a stamp."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.completion_gate import JUDGE_AGENT
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()
_PLAN = """---
title: p
status: executing
plan_id: pln-x-123456
prime_exit_criterion:
  text: a.py exists
---

# Plan

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
_JUDGED = {
    "status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md",
    "authored_by": JUDGE_AGENT, "falsifier": {"verdict": "pass", "output": "ls a.py\na.py"},
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "plan"], root)
    return root


def _run(repo: Path, criterion: dict, tests: dict) -> dict:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    run_dir = repo / RUN_DIR_ROOT / "run1"
    run_dir.mkdir(parents=True)
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path="docs/plan.md")
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/run1", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/run1/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/run1/manifest.json\n", encoding="utf-8")
    inline_review = {
        **_REVIEWED,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": tests, "criterion": criterion,
    }
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": inline_review,
         "session_id": "11111111-2222-3333-4444-555555555555"},
        repo_root=repo / ".git",
    )


def test_unjudged_met_commits_the_work_but_writes_a_halted_outcome_and_no_stamp(repo):
    out = _run(
        repo, {"status": "met", "observation": "a.py exists", "sidecar": "j.md"},
        {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"},
    )

    assert out["committed"] is True and out["sha"], out
    assert "verdict not judge-authored" in out["completion_gate"]["blockers"]
    assert out["plan_status"] == "not-stamped" and out["plan_status_reason"].startswith("completion gate:")
    assert "status: executing" in (repo / "docs" / "plan.md").read_text(encoding="utf-8")
    marker = halted_run.read_strict(record_dir(repo, "run1"))
    assert marker["status"] == halted_run.STATUS_HALTED and marker["halted"] == "completion-gate"
    assert marker["reason"].startswith("completion gate:")
    assert _git(["show", "--stat", "--format=", out["sha"]], repo).count("a.py") == 1


def test_judge_backed_met_reports_no_gate_and_writes_no_marker(repo):
    out = _run(repo, dict(_JUDGED), {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"})

    assert out["committed"] is True, out
    assert "completion_gate" not in out
    assert halted_run.read_strict(record_dir(repo, "run1")) is None
