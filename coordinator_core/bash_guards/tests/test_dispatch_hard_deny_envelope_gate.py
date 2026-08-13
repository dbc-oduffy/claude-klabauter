"""Execution evidence for the ``_is_hard_deny_envelope`` gating fix in
``coordinator_core.bash_guards.dispatch.evaluate_payload_json``: a Bash
cross-repo deny (``bump-foreign-repo-write``, registered ``fail_closed=
False``) must now be reachable through ``guard_unlock_sentinel``, exactly
like every other hard-deny envelope, because the gate is the envelope's own
``permissionDecision`` rather than the guard's crash-routing policy.

Spec backlink: state/bug-backlog/2026-08-10-cross-repo-write-boundary-
denies-on-bash-b6fd16ed9ab9.yaml, chunk 2.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

# Every test here builds real anchor/foreign git repos and drives real
# `git commit`/`git status` invocations through `dispatch.evaluate_payload_json`
# to prove the cross-repo hard-deny gate reads actual repo boundaries -- no
# mock stands in for `git -C <path>` argv parsing and real on-disk repo
# identity. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.session import guard_unlock_sentinel as gus


def _posix(p) -> str:
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


@pytest.fixture()
def repos(tmp_path):
    anchor = _init_repo(tmp_path, "anchor")
    foreign = _init_repo(tmp_path, "foreign")
    home = tmp_path / "home"
    home.mkdir()
    return {"anchor": anchor, "foreign": foreign, "home": home}


@pytest.fixture(autouse=True)
def _isolated_sentinel_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "guard-unlock-home"))
    (tmp_path / "guard-unlock-home").mkdir(exist_ok=True)
    yield


def _set_anchor(monkeypatch, repos, session_id: str) -> None:
    monkeypatch.setenv("HOME", str(repos["home"]))
    session_start.write_session_start_record(session_id, launch_cwd=str(repos["anchor"]))


def _decision(payload):
    out = dispatch.evaluate_payload_json(json.dumps(payload))
    if out is None:
        return "allow", out
    return out.get("hookSpecificOutput", {}).get("permissionDecision"), out


def _cross_repo_payload(session_id, repos):
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    return {
        "tool_name": "Bash",
        "session_id": session_id,
        "cwd": str(repos["anchor"]),
        "tool_input": {"command": cmd},
    }


class TestBashCrossRepoDenyIsSentinelClearable:
    """The chunk-2 claim: a real ``bump-foreign-repo-write`` deny (a
    ``fail_closed=False`` guard's OWN normal-path deny, not a crash) is
    now (a) annotated with the in-session-unlock line and (b) actually
    clearable by dropping the sentinel, mirroring every hard-deny guard."""

    def test_deny_advertises_the_sentinel_unlock_line(self, repos, monkeypatch):
        _set_anchor(monkeypatch, repos, "sess-deny-1")
        decision, out = _decision(_cross_repo_payload("sess-deny-1", repos))
        assert decision == "deny"
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "guard-unlock-" in reason
        assert "bump-foreign-repo-write" in reason

    def test_sentinel_clears_the_deny_and_is_consumed_once(self, repos, monkeypatch):
        _set_anchor(monkeypatch, repos, "sess-deny-2")
        sentinel = gus.sentinel_path("sess-deny-2", "bump-foreign-repo-write")
        sentinel.write_text("", encoding="utf-8")

        decision, _out = _decision(_cross_repo_payload("sess-deny-2", repos))
        assert decision == "allow"
        assert not sentinel.exists()

        # One-shot: the same command is re-denied on immediate retry.
        decision2, _out2 = _decision(_cross_repo_payload("sess-deny-2", repos))
        assert decision2 == "deny"


class TestNoOtherGuardChangesBehaviourUnderTheNewGate:
    """Every `fail_closed=False` guard whose own module never composes a
    `permissionDecision: "deny"` envelope on its normal (non-crash) path
    must observe IDENTICAL behaviour before and after dropping the
    `fail_closed and` conjunct -- `_is_hard_deny_envelope` can only differ
    for a guard that both is `fail_closed=False` AND returns a real deny,
    which the dispatch.py audit found to be exactly the two bump guards."""

    def test_block_dev_repo_sentinel_removal_advisory_stays_allow_only(self, tmp_path):
        payload = {
            "tool_name": "Bash",
            "session_id": "sess-advisory-1",
            "cwd": str(tmp_path),
            "tool_input": {"command": "rm .coordinator-dev-repo"},
        }
        decision, _out = _decision(payload)
        assert decision != "deny"

    def test_offer_git_c_rewrite_is_unaffected(self, repos, monkeypatch):
        _set_anchor(monkeypatch, repos, "sess-advisory-2")
        cmd = f"cd {_posix(repos['anchor'])} && git status"
        payload = {
            "tool_name": "Bash",
            "session_id": "sess-advisory-2",
            "cwd": str(repos["anchor"]),
            "tool_input": {"command": cmd},
        }
        decision, _out = _decision(payload)
        assert decision != "deny"
