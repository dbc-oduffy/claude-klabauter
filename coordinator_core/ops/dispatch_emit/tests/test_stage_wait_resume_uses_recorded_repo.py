"""A stage-wait refire is spooled against the run's own repo_root, and never for a run that already landed."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ask_spool import record as spool
from coordinator_core.ops.dispatch_emit import run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG, record_dir


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _park_behind_blocker(root):
    run_token.mint(root, "blocker", "s.py", session_id="sess")
    run_token.mint(root, "waiter", "s.py", session_id="sess", sizing_rel="docs/x.yaml")
    stage_wait.park(root, "waiter", ["blocker"], ["a.py"], session_id="sess")


def _spooled_args(root):
    return [
        json.loads(spool.read_entry(spool.entry_path(str(root), sid))[1])["tool_input"]["args"]
        for sid in spool.list_pending(str(root))
    ]


def test_refire_names_the_recorded_repo_root(repo):
    _park_behind_blocker(repo)
    run_token.consume(repo, "blocker")
    (args,) = _spooled_args(repo)
    assert args["repo"] == str(repo.resolve())
    assert args["sizing"] == "docs/x.yaml"
    assert args[RESUME_RUN_ID_ARG] == "waiter"


def test_refire_is_not_spooled_for_a_landed_run(repo):
    _park_behind_blocker(repo)
    marker_dir = record_dir(repo, "waiter")
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "halted.json").write_text(json.dumps({"status": "landed", "sha": "abc"}), encoding="utf-8")
    run_token.consume(repo, "blocker")
    assert _spooled_args(repo) == []


def test_refire_is_spooled_for_a_halted_run(repo):
    _park_behind_blocker(repo)
    marker_dir = record_dir(repo, "waiter")
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    run_token.consume(repo, "blocker")
    assert len(_spooled_args(repo)) == 1
