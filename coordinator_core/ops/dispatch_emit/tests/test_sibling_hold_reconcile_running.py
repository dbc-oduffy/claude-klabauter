"""sibling_hold.reconcile_running: a running run whose terminal commit is on HEAD reads landed."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.win_portability import no_console_creationflags

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import sibling_hold as sh

RUN = "ask-20261018T022939-ca3355"
PARENT = "ask-parent-20261018T022939-250228"

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


def _commit_all(root, msg):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


def _status(root, **fields):
    _write(root, c.status_rel(RUN), json.dumps({"status": "running", "parent_id": PARENT, **fields}))


def _read_status(root):
    return json.loads((root / c.status_rel(RUN)).read_text(encoding="utf-8"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _write(root, "src/a.py", "a1\n")
    _commit_all(root, "base")
    return root


def _usage(as_of):
    return json.dumps({"as_of": as_of, "run_total": {}})


def test_terminal_record_at_head_rewrites_running_as_landed(repo):
    _write(repo, f"state/warp/{RUN}/usage.json", _usage("terminal-commit"))
    _write(repo, f"state/warp/{RUN}/run.json", "{}")
    head = _commit_all(repo, "terminal")
    _status(repo)
    assert sh.reconcile_running(repo, RUN) == head
    assert _read_status(repo) == {
        "status": "landed", "sha": head, "reconciled_from": "running", "parent_id": PARENT,
    }


def test_halted_record_at_head_is_not_rewritten(repo):
    _write(repo, f"state/warp/{RUN}/usage.json", _usage("terminal-commit"))
    _write(repo, f"state/warp/{RUN}/halted.json", json.dumps({"status": "halted"}))
    _commit_all(repo, "halted record")
    _status(repo)
    assert sh.reconcile_running(repo, RUN) is None
    assert _read_status(repo)["status"] == "running"
    assert sh.predecessor_closed(repo, RUN) is None


def test_checkpoint_commits_only_leave_running(repo):
    _write(repo, "src/a.py", "a2\n")
    _commit_all(repo, "checkpoint(wave 0)")
    _status(repo)
    assert sh.reconcile_running(repo, RUN) is None
    assert _read_status(repo)["status"] == "running"


@pytest.mark.parametrize("state", ["landed", "halted"])
def test_non_running_status_is_untouched(repo, state):
    _write(repo, f"state/warp/{RUN}/usage.json", _usage("terminal-commit"))
    _commit_all(repo, "terminal")
    _write(repo, c.status_rel(RUN), json.dumps({"status": state}))
    before = (repo / c.status_rel(RUN)).read_text(encoding="utf-8")
    assert sh.reconcile_running(repo, RUN) is None
    assert (repo / c.status_rel(RUN)).read_text(encoding="utf-8") == before


def test_absent_status_file_returns_none_and_writes_nothing(repo):
    _write(repo, f"state/warp/{RUN}/usage.json", _usage("terminal-commit"))
    _commit_all(repo, "terminal")
    assert sh.reconcile_running(repo, RUN) is None
    assert not (repo / c.status_rel(RUN)).exists()


@pytest.mark.parametrize("run_id", ["../x", "a/b", "", ".."])
def test_unsafe_run_id_is_none(repo, run_id):
    assert sh.reconcile_running(repo, run_id) is None


def test_abandoned_reap_shape_is_not_landed(repo):
    _write(repo, f"state/warp/{RUN}/run.json", "{}")
    _commit_all(repo, "reap abandoned")
    _status(repo)
    assert sh.reconcile_running(repo, RUN) is None
    assert _read_status(repo)["status"] == "running"


def test_usage_not_stamped_terminal_commit_is_not_landed(repo):
    _write(repo, f"state/warp/{RUN}/usage.json", _usage("mid-run"))
    _commit_all(repo, "mid")
    _status(repo)
    assert sh.reconcile_running(repo, RUN) is None


def test_reconcile_spawns_no_process(repo, monkeypatch):
    _write(repo, f"state/warp/{RUN}/usage.json", _usage("terminal-commit"))
    head = _commit_all(repo, "terminal")
    _status(repo)

    def boom(*a, **k):
        raise AssertionError("reconcile_running spawned a process")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert sh.reconcile_running(repo, RUN) == head
