"""submit_engine_ask._slice_deny: a running predecessor whose terminal commit is on HEAD releases its siblings."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.hooks import submit_engine_ask as sea
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.win_portability import no_console_creationflags

PARENT = "ask-parent-20261018T022939-250228"
A = "ask-20261018T022939-ca3355"
B = "ask-20261018T022939-a68bdf"

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _parent(root, runs):
    rec = c.ParentRecord(parent_id=PARENT, runs=tuple(runs))
    _write(root, c.parent_record_rel(PARENT), json.dumps(rec.to_json()))


def _terminal_record_at_head(root, run_id):
    _write(root, f"state/warp/{run_id}/usage.json", json.dumps({"as_of": "terminal-commit"}))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "terminal")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "src/a.py", "a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _read_status(root, run_id):
    return json.loads((root / c.status_rel(run_id)).read_text(encoding="utf-8"))


def _incident(root):
    _parent(root, [c.SiblingRun(A, "a", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", after=(A,), status=c.STATUS_AFTER)])
    _write(root, c.status_rel(A), json.dumps({"status": "running", "parent_id": PARENT}))


def test_running_but_landed_predecessor_releases_after_sibling(repo):
    _incident(repo)
    _terminal_record_at_head(repo, A)
    assert sea._slice_deny(repo, (PARENT, B)) is None
    assert _read_status(repo, A)["status"] == "landed"


def test_running_without_record_at_head_still_holds(repo):
    _incident(repo)
    why = sea._slice_deny(repo, (PARENT, B))
    assert why is not None and "has not landed (running)" in why
    assert _read_status(repo, A)["status"] == "running"


def test_running_sibling_does_not_hold_a_queued_run(repo):
    _parent(repo, [c.SiblingRun(A, "a", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", status=c.STATUS_QUEUED)])
    _write(repo, c.status_rel(A), json.dumps({"status": "running", "parent_id": PARENT}))
    assert sea._slice_deny(repo, (PARENT, B)) is None
