"""dispatch.emit of a plan whose halted run recorded failed wave commits re-attempts them, with no caller-passed kwargs."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halt_refire import commit_retry_from_reason
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_REL = "docs/plans/p.md"
SIZING_REL = "state/sizings/2026-10-24-p-aaaaaa.yaml"
RUN_ID = "warp-2026-10-24-p-aaaaaa-20261024T010101-abcdef"
REFUSAL = "wave 1: claimed by live session 65b9230f"
REASON = (
    "wave commit failed, dependents held (the terminal commit lands the wave): "
    + REFUSAL
    + " [an unedited Workflow resume replays this cached refusal; to re-attempt, re-emit the plan with dispatch.emit (it pins the Checkpoint-Base and the refusal from this halt), then resume]"
)

_PLAN = f"""---
title: "p"
sizing_object: {SIZING_REL}
---

# p

## Tasks

```yaml plan-tasks
- id: A1
  title: tA1
  change_kind: doc-edit
  surface: docs/x/a1.md
  writes:
    - docs/x/a1.md
  queue_scope: project
  disposition: open
  body: |
    Do A1.
```
"""


def _git(root, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "claude")
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    (root / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    _git(root, "add", "--", PLAN_REL)
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _halt(root, reason, base):
    rec = root / RECORD_DIR_ROOT / RUN_ID
    rec.mkdir(parents=True)
    marker = {"status": "halted", "halted": "dependents_unstarted", "sizing_rel": SIZING_REL}
    if reason:
        marker["reason"] = reason
    marker["run_base_sha"] = base
    (rec / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def _emit(root) -> str:
    out = root / "scratch" / "out" / "emitted.workflow.mjs"
    out.parent.mkdir(parents=True, exist_ok=True)
    result = _dispatch_emit({"plan_path": str(root / PLAN_REL), "output_path": str(out)}, repo_root=root)
    assert result["ok"] is True, result
    return out.read_text(encoding="utf-8")


def test_the_reason_the_finaliser_writes_parses_to_a_wave_map():
    assert commit_retry_from_reason(REASON) == {1: REFUSAL}
    two = REASON.replace(REFUSAL, REFUSAL + "; wave 2: no committer reply")
    assert commit_retry_from_reason(two) == {1: REFUSAL, 2: "wave 2: no committer reply"}
    assert commit_retry_from_reason("seam check failed at wave 1") == {}
    assert commit_retry_from_reason(None) == {}


def test_a_halted_run_with_a_commit_failure_re_emits_with_the_retry_and_its_base(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _halt(repo, REASON, base)
    (repo / "peer.txt").write_text("p", encoding="utf-8")
    _git(repo, "add", "--", "peer.txt")
    _git(repo, "commit", "-q", "-m", "peer moved head")

    script = _emit(repo)

    assert "Re-attempt the commit now" in script and REFUSAL in script
    assert f'let _runBase = "{base}";' in script or f"let _runBase = '{base}';" in script


def test_a_halted_run_without_a_commit_failure_emits_no_retry(repo):
    _halt(repo, "run halted by stop rule", _git(repo, "rev-parse", "HEAD"))
    assert "_commitRetry" not in _emit(repo)


def test_no_halted_run_emits_no_retry(repo):
    assert "_commitRetry" not in _emit(repo)


def test_a_recorded_base_that_does_not_reach_head_emits_no_retry(repo):
    _halt(repo, REASON, "a" * 40)
    assert "_commitRetry" not in _emit(repo)
