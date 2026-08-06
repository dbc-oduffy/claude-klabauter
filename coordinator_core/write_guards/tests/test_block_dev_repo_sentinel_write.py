"""Tests for coordinator_core.write_guards.block_dev_repo_sentinel_write.

Spec backlink: coordinator_core/write_guards/block_dev_repo_sentinel_write.py
Precedent test shape: coordinator_core/write_guards/tests/test__sentinel_write_guard.py
"""

from __future__ import annotations

from coordinator_core.write_guards import block_dev_repo_sentinel_write as guard


SENTINEL = ".coordinator-dev-repo"


def _payload(tool_name, file_path):
    return {"tool_name": tool_name, "tool_input": {"file_path": file_path}}


def _advisory_reason(out):
    assert out is not None, "expected an advisory envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    return hso["additionalContext"]


class TestAdvisesEachMatcher:
    def test_write_advises(self):
        _advisory_reason(guard.check(_payload("Write", "/repo/%s" % SENTINEL)))

    def test_edit_advises(self):
        _advisory_reason(guard.check(_payload("Edit", "/repo/%s" % SENTINEL)))

    def test_multiedit_advises(self):
        _advisory_reason(guard.check(_payload("MultiEdit", "/repo/%s" % SENTINEL)))

    def test_notebookedit_advises(self):
        payload = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/repo/%s" % SENTINEL}}
        _advisory_reason(guard.check(payload))

    def test_case_varied_basename_advises(self):
        _advisory_reason(guard.check(_payload("Write", "/repo/.COORDINATOR-DEV-REPO")))


class TestPassSet:
    def test_ordinary_repo_root_file_allows(self):
        assert guard.check(_payload("Write", "/repo/README.md")) is None

    def test_non_matcher_tool_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": {"command": "rm x"}}) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None

    def test_missing_path_allows(self):
        assert guard.check({"tool_name": "Write", "tool_input": {}}) is None

    def test_near_miss_filename_allows(self):
        assert guard.check(_payload("Write", "/repo/%s-typo" % SENTINEL)) is None


class TestOverrideEnvVar:
    def test_override_allows(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL", "1")
        assert guard.check(_payload("Write", "/repo/%s" % SENTINEL)) is None


class TestAdvisoryMessageDiscipline:
    def test_advisory_reason_never_names_the_sentinel(self):
        reason = _advisory_reason(guard.check(_payload("Write", "/repo/%s" % SENTINEL)))
        assert SENTINEL not in reason

    def test_advisory_reason_advertises_override(self):
        reason = _advisory_reason(guard.check(_payload("Write", "/repo/%s" % SENTINEL)))
        assert "COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL=1" in reason
