"""submit_engine_ask._slice_deny: a halted predecessor whose terminal commit is on HEAD releases its dependent
though its close recorded no judge verdict; an unlanded one still holds."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.hooks import submit_engine_ask as sea
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.win_portability import no_console_creationflags

PARENT = "ask-parent-20261113T205204-375ee4"
A = "ask-20261113T205204-baf4ed"
B = "ask-20261113T205204-839b9d"

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
    rec = c.ParentRecord(
        parent_id=PARENT,
        runs=(c.SiblingRun(A, "a", status=c.STATUS_RUNNING), c.SiblingRun(B, "b", after=(A,), status=c.STATUS_AFTER)),
    )
    _write(root, c.parent_record_rel(PARENT), json.dumps(rec.to_json()))
    _write(root, c.status_rel(A), json.dumps({"status": "halted", "parent_id": PARENT}))
    return root


def _land_terminal_commit(root):
    _write(root, f"state/warp/{A}/usage.json", json.dumps({"as_of": "terminal-commit"}))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "terminal")


def test_landed_upstream_with_no_verdict_releases_dependent_with_advisory(repo):
    _land_terminal_commit(repo)
    assert sea._slice_deny(repo, (PARENT, B)) is None
    assert "no judge verdict" in sea._slice_advisory(repo, (PARENT, B))


def test_unlanded_halted_upstream_still_holds(repo):
    why = sea._slice_deny(repo, (PARENT, B))
    assert why is not None and "halted" in why
    assert sea._slice_advisory(repo, (PARENT, B)) == ""


def test_halt_record_at_head_is_not_a_landing(repo):
    _land_terminal_commit(repo)
    _write(repo, f"state/warp/{A}/halted.json", json.dumps({"status": "halted"}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "halt")
    assert sea._slice_deny(repo, (PARENT, B)) is not None
