"""A halted predecessor closed through `plan_status_transition stamp-implemented` releases its sibling, and a
held slice marked superseded by a named run is neither released nor fired, through the hook's `evaluate`
with the real fan-out, the real emit and the real stamp op on a tmp git repo.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run, run_close
from coordinator_core.ops.plan_status_transition import main as stamp_main
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SID = "sess-sibling-hold-e2e"
VERBATIM = "fix the parser and also rename the exporter, they are unrelated jobs"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}
_PLAN = """---
title: T
status: executing
owner: x
sizing_object: {sizing}
---

# Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: only row
  change_kind: code-edit
  surface: a.py
  writes:
    - a.py
  disposition: coded
  body: |
    do it
```
"""
_SEQUENCED = [{"ask": "one", "writes": ["a"]}, {"ask": "two", "writes": ["a"]}]


def _git(root: Path, *args: str) -> None:
    r = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, env=_ENV, timeout=30,
        **no_console_creationflags(),
    )
    assert r.returncode == 0, r.stderr


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    hub = tmp_path / "hub" / SID
    hub.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn(VERBATIM, session_id=SID)["ok"]
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _submit(root: Path, args: dict) -> dict:
    return mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })


def _hso(env: dict) -> dict:
    return env["hookSpecificOutput"]


def _reply_json(env: dict) -> dict:
    flat = json.dumps(env).replace('\\"', '"')
    start = flat.index("Reply: ") + len("Reply: ")
    depth, end = 0, start
    for i in range(start, len(flat)):
        depth += flat[i] == "{"
        depth -= flat[i] == "}"
        if depth == 0:
            end = i + 1
            break
    return json.loads(flat[start:end])


def _parent(root: Path, parent_id: str) -> c.ParentRecord:
    return c.ParentRecord.from_json(json.loads((root / c.parent_record_rel(parent_id)).read_text(encoding="utf-8")))


def _status(root: Path, run_id: str) -> str:
    return json.loads((root / c.status_rel(run_id)).read_text(encoding="utf-8"))["status"]


def _sequenced_pair(root: Path):
    """Submit two sequenced asks; the first is fired (bound) and halted, with its halt record written."""
    reply = _reply_json(_submit(root, {"asks": _SEQUENCED}))
    parent = _parent(root, reply["parent_id"])
    first, second = parent.runs
    assert second.after == (first.run_id,)
    assert run_close.sibling_release(root, first.run_id, None)["released"] == []
    halted_run.write_halt_marker(
        root / "state" / "warp" / first.run_id, halted="gate", sizing_rel=first.slice_sizing
    )
    assert _status(root, first.run_id) == "halted"
    return first, second


def _stamp_plan_for(root: Path, sizing_rel: str) -> Path:
    plan = root / "p.md"
    plan.write_text(_PLAN.format(sizing=sizing_rel), encoding="utf-8")
    _git(root, "add", "--", "p.md")
    _git(root, "commit", "-q", "-m", "initial")
    return plan


def test_a_halted_predecessor_closed_by_stamp_implemented_releases_the_successor(repo, capsys):
    first, second = _sequenced_pair(repo)

    denied = _submit(repo, {"sizing": second.slice_sizing})
    assert _hso(denied)["permissionDecision"] == "deny"
    assert "halted" in _hso(denied)["permissionDecisionReason"]

    plan = _stamp_plan_for(repo, first.slice_sizing)
    capsys.readouterr()
    assert stamp_main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0

    assert "status: implemented" in plan.read_text(encoding="utf-8")
    assert json.loads((repo / c.status_rel(first.run_id)).read_text(encoding="utf-8"))[c.CLOSED_BY_KEY] == "p.md"
    assert warp_fire.fire_command(second.slice_sizing) in capsys.readouterr().out

    allowed = _submit(repo, {"sizing": second.slice_sizing})
    assert _hso(allowed)["permissionDecision"] == "allow", allowed
    assert _status(repo, second.run_id) == "running"


def test_a_superseded_held_slice_is_reported_not_released_and_its_fire_is_denied(repo, capsys):
    first, second = _sequenced_pair(repo)

    fresh = _submit(repo, {"ask": "two, resubmitted", "source": "relayed", "supersedes": second.run_id})
    assert _hso(fresh)["permissionDecision"] == "allow", fresh
    marker = json.loads((repo / c.superseded_rel(second.run_id)).read_text(encoding="utf-8"))
    superseding = marker[c.SUPERSEDED_BY_KEY]
    assert superseding not in (first.run_id, second.run_id)
    assert f"Marked {second.run_id} superseded by {superseding}." in json.dumps(fresh)

    plan = _stamp_plan_for(repo, first.slice_sizing)
    capsys.readouterr()
    assert stamp_main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 0
    out = capsys.readouterr().out
    assert warp_fire.fire_command(second.slice_sizing) not in out
    assert superseding in out

    close = run_close.sibling_release(repo, first.run_id, None)
    assert close["released"] == [] and close["blocked"] == []
    assert close["superseded"] == [{"run_id": second.run_id, "superseded_by": superseding}]

    denied = _submit(repo, {"sizing": second.slice_sizing})
    assert _hso(denied)["permissionDecision"] == "deny"
    assert superseding in _hso(denied)["permissionDecisionReason"]
    assert not (repo / c.status_rel(second.run_id)).exists()
