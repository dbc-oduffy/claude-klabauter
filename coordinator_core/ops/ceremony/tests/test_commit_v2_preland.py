"""The rebuilt `ceremony.commit_v2` pre-land: a 2-path land keeps exactly the inventory's phases.

Structure only, no timing: the 500ms exit is measured by the paired-verdict instrument.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony.commit_v2_phases import PhaseLedger
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-4333-8444-555555555555"

KEPT_PHASES = {
    "param_validation",
    "main_commit_refusal",
    "head_classification",
    "resolve_session_id",
    "claim_lookup",
    "pre_commit_gates",
    "eol_drift",
    "apply_missing_trailers",
    "skill_weight_check",
    "publish_allowlist_drift",
    "split_process_paths",
    "land.product",
    "release_committed_claims",
    "record_ledger",
    "post_land_reply",
    "unbracketed",
}


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "a.py").write_text("v1\n", encoding="utf-8")
    (root / ".gitignore").write_text("junk.log\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "a.py").write_text("v2\n", encoding="utf-8")
    (root / "b.py").write_text("new\n", encoding="utf-8")
    return root


def _call(repo: Path, **extra) -> dict:
    params = {"paths": ["a.py", "b.py"], "message": "land two\n", "session_id": SID, **extra}
    return commit_v2._handler(params, repo_root=repo / ".git")


def test_two_path_commit_lands_with_the_session_id_trailer(repo):
    result = _call(repo)

    assert result["committed"] is True
    assert _git(["rev-parse", "HEAD"], repo) == result["sha"]
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"a.py", "b.py"}
    assert f"Session-Id: {SID}" in _git(["log", "-1", "--format=%B"], repo)
    assert {"committed", "sha", "tree", "warnings"} <= set(result)
    assert not {"own_left_behind", "stopped_run_residue"} & set(result)


def test_the_phase_row_names_exactly_the_kept_set(repo, monkeypatch):
    seen: dict = {}
    real_emit = PhaseLedger.emit

    def _spy(self, *args, **kwargs):
        seen["phases"] = set(self.phases_ms())
        return real_emit(self, *args, **kwargs)

    monkeypatch.setattr(PhaseLedger, "emit", _spy)
    assert _call(repo)["committed"] is True
    assert seen["phases"] == KEPT_PHASES


@pytest.mark.parametrize(
    ("param", "owner"),
    [
        ("carry_stopped_runs", "dispatch.reap_abandoned_runs"),
        ("carry_own_left_behind", "own_carry.own_carry_params"),
    ],
)
def test_a_dropped_param_set_true_is_refused_naming_its_owner(repo, param, owner):
    head = _git(["rev-parse", "HEAD"], repo)

    refused = _call(repo, **{param: True})

    assert refused["committed"] is False
    assert owner in refused["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head

    accepted = _call(repo, **{param: False})
    assert accepted["committed"] is True


def test_an_abandon_set_before_the_land_moves_no_ref_and_leaves_no_index_lock(repo):
    head = _git(["rev-parse", "HEAD"], repo)
    event = threading.Event()
    event.set()
    token = dispatch_abandon.ACTIVE.set(event)
    try:
        result = _call(repo)
    finally:
        dispatch_abandon.ACTIVE.reset(token)

    assert result["committed"] is False
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert not (repo / ".git" / "index.lock").exists()


def test_a_gitignored_untracked_path_still_refuses_and_a_cached_removal_still_lands(repo):
    (repo / "junk.log").write_text("x\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    refused = _call(repo, paths=["a.py", "junk.log"])
    assert refused["committed"] is False
    assert "junk.log" in refused["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head

    _git(["rm", "-q", "--cached", "--", "README.md"], repo)
    landed = _call(repo, paths=["README.md"], message="remove README.md from the tree\n")
    assert landed["committed"] is True, landed
    assert "README.md" not in _git(["ls-tree", "-r", "--name-only", "HEAD"], repo).split()
    assert (repo / "README.md").exists()
