"""AC5 runtime probes for the bash guards DR-277 demoted and the 2026-10-04 amendment
restored: declared class and RUNTIME behaviour must agree, probed through the real
entrypoint `dispatch.evaluate_payload_json` (not `module.check`), so an earlier entry
swallowing the slot is caught.

At guard level strict (the author default, pinned suite-wide) each guard denies with
its own text; at warn (the consumer default) the policy point turns the deny into an
advisory.

Spec backlink: pln-apply-the-guard-class-census-u-4cae4a, AC5.
"""
from __future__ import annotations

import json

import pytest

from coordinator_core import machine_profile

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import block_noncanonical_branch_creation
from coordinator_core.bash_guards import block_subagent_plan_body_bash_write
from coordinator_core.bash_guards import check_raw_pid_liveness
from coordinator_core.bash_guards import block_dev_repo_sentinel_removal


def _payload_dict(command, **extra):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    p.update(extra)
    return p


def _evaluate(payload_dict, **kwargs):
    return dispatch.evaluate_payload_json(json.dumps(payload_dict), **kwargs)


def _assert_deny(out, *, must_contain: str):
    assert out is not None, "dispatch returned ALLOW, expected deny"
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "deny", "expected a deny envelope: %r" % out
    assert must_contain in json.dumps(out), (
        "deny envelope lacks this guard's own text -- possibly an incumbent "
        "guard's envelope: %r" % out
    )


def _assert_advisory_not_deny(out, *, must_contain: str):
    assert out is not None, "dispatch returned ALLOW, expected advisory"
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") != "deny", "still a deny: %r" % out
    assert hso["additionalContext"].startswith(machine_profile.ADVISORY_PREFIX), out
    assert must_contain in json.dumps(out), out


class TestBlockNoncanonicalBranchCreation:
    @pytest.fixture(autouse=True)
    def _hazard_repo(self, monkeypatch):
        monkeypatch.setattr(
            block_noncanonical_branch_creation, "resolve_git_root", lambda cwd=None: "/repo"
        )
        monkeypatch.setattr(
            block_noncanonical_branch_creation, "_is_hazard_repo", lambda git_root: True
        )
        # block-topic-branch runs earlier in the chain and denies the same
        # command; this class probes the noncanonical guard's own band.
        from coordinator_core.bash_guards import block_topic_branch

        monkeypatch.setattr(block_topic_branch, "check", lambda payload: None)

    def test_denies_through_dispatch(self):
        out = _evaluate(_payload_dict("git branch bad-name"))
        _assert_deny(out, must_contain="branch")

    def test_consumer_warn_level_turns_the_deny_into_an_advisory(self, monkeypatch):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "warn")
        machine_profile.reset_cache()
        out = _evaluate(_payload_dict("git branch bad-name"))
        _assert_advisory_not_deny(out, must_contain="branch")


class TestBlockSubagentPlanBodyBashWrite:
    _WRITE_CMD = 'echo "in progress" >> docs/plans/2026-07-30-x.md'

    def _stub(self, monkeypatch, subagent_type="coordinator:executor"):
        monkeypatch.setattr(
            block_subagent_plan_body_bash_write, "resolve_git_root", lambda cwd: "/fake/git-root"
        )
        monkeypatch.setattr(
            block_subagent_plan_body_bash_write,
            "_resolve_subagent_identity",
            lambda raw, session: "deadbeef0123",
        )
        monkeypatch.setattr(
            block_subagent_plan_body_bash_write,
            "_read_backpointer_subagent_type",
            lambda git_root, agent_id, **kw: subagent_type,
        )
        monkeypatch.setattr(
            block_subagent_plan_body_bash_write, "_write_block_log", lambda *a, **kw: None
        )

    def test_fires_deny_through_dispatch(self, monkeypatch):
        self._stub(monkeypatch)
        out = _evaluate(_payload_dict(self._WRITE_CMD, agent_id="deadbeef0123"))
        _assert_deny(out, must_contain="coordinator:executor")


class TestCheckRawPidLiveness:
    def test_fires_deny_through_dispatch(self, monkeypatch):
        monkeypatch.delenv(check_raw_pid_liveness._OVERRIDE_ENV, raising=False)
        out = _evaluate(_payload_dict("ps -p 12345"))
        _assert_deny(out, must_contain="session-liveness-cli")


class TestBlockDevRepoSentinelRemoval:
    SENTINEL = ".coordinator-dev-repo"

    def test_fires_deny_through_dispatch(self):
        out = _evaluate(_payload_dict("rm %s" % self.SENTINEL))
        _assert_deny(out, must_contain="dev-repo guard")
