"""
test_push_outstanding_p4_leg.py — pytest coverage for the p4 leg C3 adds to
`coordinator_core.ops.push_outstanding`.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md § C3, § D4.

Scope: the leg's own gating/wiring logic in `push_outstanding.py` --
`_is_p4_repo`, `_p4_leg_precheck`, `_p4_leg_execute`, the `session_id`
parameter, and the `push.outstanding.p4`/`push.outstanding.p4:no-session`
telemetry arms. `coordinator_core.p4.shelve`'s own reconcile/revert/shelve
sequence is `test_shelve.py`'s scope, not this file's -- here it is
monkeypatched at the module boundary.

Asserts both named arms (D4/C3 row body): a session-supplied invocation
shelves; a session-less invocation (the cadence shape) takes zero p4
spawns and names the skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.push_outstanding as push_outstanding_mod
import coordinator_core.p4.session_change as p4_session_change_mod
import coordinator_core.p4.shelve as p4_shelve_mod
import coordinator_core.p4.workspace as p4_workspace_mod
from coordinator_core.ops.ceremony.push import PushOutcome
from coordinator_core.ops.push_outstanding import push_outstanding

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=no_window,
    )


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed", encoding="utf-8")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/p4-leg"], repo)
    return repo


def _identity() -> p4_workspace_mod.P4Identity:
    return p4_workspace_mod.P4Identity(
        port="p4.example.com:1666", user="bob", client="bob-ws", client_root="/depot/root"
    )


def _fail_if_called(name):
    def _inner(*a, **kw):
        raise AssertionError(f"{name} must not be called")

    return _inner


class _RecordingTelemetry:
    """Captures every `record_op_latency` call so a test can assert which
    arm/op fired without touching the real telemetry sink."""

    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(
            "coordinator_core.telemetry.op_latency.record_op_latency",
            lambda **kw: self.calls.append(kw),
        )


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path, "repo")


class TestGitOnlyRepoNeverRunsTheLeg:
    def test_git_only_repo_skips_p4_leg_entirely(self, monkeypatch, repo):
        monkeypatch.setattr(push_outstanding_mod, "_is_p4_repo", lambda root: False)
        monkeypatch.setattr(
            p4_workspace_mod, "identity", _fail_if_called("workspace.identity")
        )
        monkeypatch.setattr(
            push_outstanding_mod,
            "push_with_retry",
            lambda *a, **kw: PushOutcome(exit_code=0, skipped=["push:nothing-outstanding"]),
        )

        outcome = push_outstanding(repo, session_id="sid-1")

        assert outcome.exit_code == 0


class TestSessionlessInvocationTakesZeroP4SpawnsAndNamesTheSkip:
    def test_no_session_id_records_no_session_arm(self, monkeypatch, repo):
        monkeypatch.setattr(push_outstanding_mod, "_is_p4_repo", lambda root: True)
        monkeypatch.setattr(
            p4_workspace_mod, "identity", _fail_if_called("workspace.identity")
        )
        monkeypatch.setattr(
            p4_workspace_mod, "session_change", _fail_if_called("workspace.session_change")
        )
        telemetry = _RecordingTelemetry(monkeypatch)

        outcome = push_outstanding(repo, session_id=None)

        assert outcome.exit_code == 0  # nothing-outstanding (HEAD == upstream, no remote at all -> falls through)
        no_session_calls = [c for c in telemetry.calls if c["op"] == push_outstanding_mod._ARM_P4_NO_SESSION]
        assert len(no_session_calls) == 1


class TestSessionSuppliedInvocationShelves:
    def test_shelve_leg_runs_after_the_git_leg(self, monkeypatch, repo):
        monkeypatch.setattr(push_outstanding_mod, "_is_p4_repo", lambda root: True)
        monkeypatch.setattr(
            push_outstanding_mod, "session_dir", lambda sid, cwd=None: str(repo / "sdir")
        )
        monkeypatch.setattr(
            p4_workspace_mod,
            "session_change",
            lambda sdir: {
                "p4_change": 101,
                "p4_base_sha": "basesha",
                "p4_shelved_at": None,
                "p4_shelved_sha": None,
            },
        )
        monkeypatch.setattr(p4_workspace_mod, "identity", lambda repo_key: _identity())
        monkeypatch.setattr(
            p4_session_change_mod, "_resolve_repo_key", lambda root: "p4-studio/fifa-main"
        )
        monkeypatch.setattr(
            p4_session_change_mod, "ensure_session_change", lambda root, sid: 101
        )
        # No remote is configured on this fixture repo, so the real
        # `push_with_retry` would itself decline `push:no-remote` -- which
        # would then (correctly) gate the p4 leg off and defeat this test's
        # own purpose. Force the git leg to see a genuine outstanding-work
        # decision and a landed push instead.
        monkeypatch.setattr(
            push_outstanding_mod, "_upstream_sha", lambda root, branch: "somethingelse"
        )
        monkeypatch.setattr(
            push_outstanding_mod,
            "push_with_retry",
            lambda *a, **kw: PushOutcome(exit_code=0, acted=["push"], pushed_range="a..b", pushed_count=1),
        )

        shelve_calls = []

        def fake_shelve_outstanding(root, sid, sdir, identity, cl, base_sha):
            shelve_calls.append((root, sid, sdir, identity, cl, base_sha))
            return p4_shelve_mod.ShelveOutcome(ok=True, paths=["a.uasset"], shelved_sha="deadbeef", cl=cl)

        monkeypatch.setattr(p4_shelve_mod, "shelve_outstanding", fake_shelve_outstanding)
        telemetry = _RecordingTelemetry(monkeypatch)

        outcome = push_outstanding(repo, session_id="sid-1")

        assert outcome.exit_code == 0
        assert len(shelve_calls) == 1
        assert shelve_calls[0][1] == "sid-1"
        assert shelve_calls[0][4] == 101
        assert shelve_calls[0][5] == "basesha"
        p4_arm_calls = [c for c in telemetry.calls if c["op"] == push_outstanding_mod._ARM_P4]
        assert len(p4_arm_calls) == 1
        assert p4_arm_calls[0]["outcome"] == "ok"

    def test_already_shelved_sha_is_a_zero_spawn_noop(self, monkeypatch, repo):
        head = push_outstanding_mod.head_sha(repo)
        monkeypatch.setattr(push_outstanding_mod, "_is_p4_repo", lambda root: True)
        monkeypatch.setattr(
            push_outstanding_mod, "session_dir", lambda sid, cwd=None: str(repo / "sdir")
        )
        monkeypatch.setattr(
            p4_workspace_mod,
            "session_change",
            lambda sdir: {
                "p4_change": 101,
                "p4_base_sha": "basesha",
                "p4_shelved_at": "2026-09-12T00:00:00+00:00",
                "p4_shelved_sha": head,
            },
        )
        monkeypatch.setattr(p4_workspace_mod, "identity", _fail_if_called("workspace.identity"))
        monkeypatch.setattr(
            p4_session_change_mod,
            "ensure_session_change",
            _fail_if_called("session_change.ensure_session_change"),
        )
        monkeypatch.setattr(
            p4_shelve_mod, "shelve_outstanding", _fail_if_called("shelve.shelve_outstanding")
        )

        outcome = push_outstanding(repo, session_id="sid-1")

        assert outcome.exit_code == 0


class TestGatedOnNoRemoteNeverNoUpstream:
    def test_no_remote_outcome_skips_the_p4_leg(self, monkeypatch, repo):
        monkeypatch.setattr(push_outstanding_mod, "_is_p4_repo", lambda root: True)
        monkeypatch.setattr(
            push_outstanding_mod, "session_dir", lambda sid, cwd=None: str(repo / "sdir")
        )
        monkeypatch.setattr(
            p4_workspace_mod,
            "session_change",
            lambda sdir: {
                "p4_change": 101,
                "p4_base_sha": "basesha",
                "p4_shelved_at": None,
                "p4_shelved_sha": None,
            },
        )
        # A genuinely different sha, so the git leg's zero-spawn no-op
        # return is NOT taken and `push_with_retry` runs -- monkeypatched
        # here to report the `push:no-remote` outcome the leg must gate on.
        monkeypatch.setattr(
            push_outstanding_mod,
            "push_with_retry",
            lambda *a, **kw: PushOutcome(exit_code=0, skipped=["push:no-remote"]),
        )
        monkeypatch.setattr(
            push_outstanding_mod, "_upstream_sha", lambda root, branch: "somethingelse"
        )
        monkeypatch.setattr(p4_workspace_mod, "identity", _fail_if_called("workspace.identity"))
        monkeypatch.setattr(
            p4_session_change_mod,
            "ensure_session_change",
            _fail_if_called("session_change.ensure_session_change"),
        )
        monkeypatch.setattr(
            p4_shelve_mod, "shelve_outstanding", _fail_if_called("shelve.shelve_outstanding")
        )

        outcome = push_outstanding(repo, session_id="sid-1")

        assert "push:no-remote" in outcome.skipped
