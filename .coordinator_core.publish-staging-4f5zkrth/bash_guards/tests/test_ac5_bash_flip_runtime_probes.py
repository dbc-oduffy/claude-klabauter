"""AC5 runtime probes for the four bash-side CONFINEMENT_DENY -> ADVISORY_REWRITE
flips (docs/plans/2026-08-06-apply-guard-class-census.md, C13):
`block_noncanonical_branch_creation`, `block_subagent_plan_body_bash_write`,
`check_raw_pid_liveness`, `block_dev_repo_sentinel_removal`.

Every probe goes through the REAL entrypoint,
`coordinator_core.bash_guards.dispatch.evaluate_payload_json`, never
`module.check(payload)` directly -- bypassing the dispatcher's registration
order (a hand-built literal list, see `dispatch.py`'s own docstring) would
miss a swallowed slot (an earlier CONFINEMENT_DENY or ADVISORY_REWRITE
entry shadowing this guard's own envelope).

Each of the four gets TWO probes:
  (a) the FORMER deny input now yields an advisory envelope (no
      `permissionDecision: "deny"`), carrying THIS guard's own text.
  (b) a CRASH-PATH probe -- `fail_closed=False` (this flip's second,
      distinct semantics change per `dispatch.py`'s own registration
      comment) means a `check()` that raises now routes to a silently
      swallowed ALLOW instead of a deny. Confirmed by monkeypatching the
      registered check function to raise and observing
      `evaluate_payload_json` return None (ALLOW), not a deny.

Spec backlink: pln-apply-the-guard-class-census-u-4cae4a, AC5.
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import block_noncanonical_branch_creation
from coordinator_core.bash_guards import block_subagent_plan_body_bash_write
from coordinator_core.bash_guards import check_raw_pid_liveness


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


def _assert_advisory_not_deny(out, *, must_contain: str):
    assert out is not None, "flip regression: dispatch returned ALLOW, expected advisory"
    hso = out["hookSpecificOutput"]
    assert hso.get("permissionDecision") != "deny", (
        "flip did NOT take effect: dispatch still returned a deny "
        "envelope: %r" % out
    )
    rendered = json.dumps(out)
    assert must_contain in rendered, (
        "advisory envelope came back but without this guard's own "
        "distinguishing text -- possibly an INCUMBENT guard's envelope "
        "swallowed the slot instead: %r" % out
    )


class TestBlockNoncanonicalBranchCreation:
    @pytest.fixture(autouse=True)
    def _hazard_repo(self, monkeypatch):
        monkeypatch.setattr(
            block_noncanonical_branch_creation, "resolve_git_root", lambda cwd=None: "/repo"
        )
        monkeypatch.setattr(
            block_noncanonical_branch_creation, "_is_hazard_repo", lambda git_root: True
        )

    def test_former_deny_now_advises_through_dispatch(self):
        out = _evaluate(_payload_dict("git branch bad-name"))
        _assert_advisory_not_deny(out, must_contain="branch")

    def test_crash_path_now_silently_allows(self, monkeypatch):
        def _boom(payload):
            raise RuntimeError("simulated crash inside block_noncanonical_branch_creation")

        # `dispatch.py` binds each guard's `check` at IMPORT time
        # (`from ... import check as _check_X`) -- the chain's own lambda
        # closures reference that bound name, not `module.check`, so
        # patching `module.check` alone leaves the registered chain
        # unaffected. Patch the name dispatch actually calls.
        monkeypatch.setattr(dispatch, "_check_block_noncanonical_branch_creation", _boom)
        out = _evaluate(_payload_dict("git branch bad-name"))
        assert out is None, (
            "fail_closed=False expected a silent ALLOW on crash; got %r instead" % out
        )


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

    def test_former_deny_now_advises_through_dispatch(self, monkeypatch):
        self._stub(monkeypatch)
        out = _evaluate(_payload_dict(self._WRITE_CMD, agent_id="deadbeef0123"))
        _assert_advisory_not_deny(out, must_contain="coordinator:executor")

    def test_crash_path_now_silently_allows(self, monkeypatch):
        self._stub(monkeypatch)

        def _boom(payload):
            raise RuntimeError("simulated crash inside block_subagent_plan_body_bash_write")

        monkeypatch.setattr(dispatch, "_check_plan_body_bash_write", _boom)
        out = _evaluate(_payload_dict(self._WRITE_CMD, agent_id="deadbeef0123"))
        assert out is None, (
            "fail_closed=False expected a silent ALLOW on crash; got %r instead" % out
        )


class TestCheckRawPidLiveness:
    def test_former_deny_now_advises_through_dispatch(self, monkeypatch):
        monkeypatch.delenv(check_raw_pid_liveness._OVERRIDE_ENV, raising=False)
        out = _evaluate(_payload_dict("ps -p 12345"))
        _assert_advisory_not_deny(out, must_contain="session-liveness-cli")

    def test_crash_path_now_silently_allows(self, monkeypatch):
        monkeypatch.delenv(check_raw_pid_liveness._OVERRIDE_ENV, raising=False)

        def _boom(payload):
            raise RuntimeError("simulated crash inside check_raw_pid_liveness")

        monkeypatch.setattr(dispatch, "_check_raw_pid_liveness", _boom)
        out = _evaluate(_payload_dict("ps -p 12345"))
        assert out is None, (
            "fail_closed=False expected a silent ALLOW on crash; got %r instead" % out
        )


class TestBlockDevRepoSentinelRemoval:
    SENTINEL = ".coordinator-dev-repo"

    def test_former_deny_now_advises_through_dispatch(self):
        out = _evaluate(_payload_dict("rm %s" % self.SENTINEL))
        _assert_advisory_not_deny(out, must_contain="dev-repo guard")

    def test_crash_path_now_silently_allows(self, monkeypatch):
        def _boom(payload):
            raise RuntimeError("simulated crash inside block_dev_repo_sentinel_removal")

        monkeypatch.setattr(dispatch, "_check_dev_repo_sentinel_removal_advisory", _boom)
        out = _evaluate(_payload_dict("rm %s" % self.SENTINEL))
        assert out is None, (
            "fail_closed=False expected a silent ALLOW on crash; got %r instead" % out
        )
