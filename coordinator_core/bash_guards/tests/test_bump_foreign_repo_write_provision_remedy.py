"""A subagent Bash write into a provisioned sibling target is still bumped, and the bump carries
the one advisory line; the route-remedy tail the legacy deny reason ended in is no longer
delivered (the bump guard is advisory-by-ruling: 307ea22403 DR-475, classified in b17ec35251)."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard
from coordinator_core.bash_guards._write_bump_message import CROSS_REPO_WARN_RISK
from coordinator_core.bash_guards.tests.test_bump_foreign_repo_write import (
    _clean_bump_env,  # noqa: F401 -- autoused fixture
    _commit_advisory_off,  # noqa: F401 -- autoused fixture
    _posix,
    _reset_git_root_cache,  # noqa: F401 -- autoused fixture
    _set_anchor,
    repos,  # noqa: F401 -- fixture
)
from coordinator_core.ops.sibling_repo_provision_contract import MARKER_NAME, ROUTE_REMEDY
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "sess-remedy"
_PAYLOAD = {"agent_id": "deadbeef0123", "session_id": _SID}


def _reason(result) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


def _run(repos, monkeypatch):
    _set_anchor(monkeypatch, repos, _SID)
    cmd = f"git -C {_posix(repos['foreign'])} commit --allow-empty -m x"
    return _enveloped(guard.check_bump_foreign_repo_write)(cmd, _SID, str(repos["anchor"]), _PAYLOAD)


def test_marked_target_is_bumped_with_the_advisory_line_and_no_remedy_tail(repos, monkeypatch):
    (repos["foreign"] / ".git" / MARKER_NAME).write_text(json.dumps({"name": "foreign"}), encoding="utf-8")
    result = _run(repos, monkeypatch)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert _reason(result) == "Advisory: " + CROSS_REPO_WARN_RISK
    assert ROUTE_REMEDY not in _reason(result)


def test_failing_contract_import_leaves_verdict_and_reason_unchanged(repos, monkeypatch):
    (repos["foreign"] / ".git" / MARKER_NAME).write_text(json.dumps({"name": "foreign"}), encoding="utf-8")
    baseline = _run(repos, monkeypatch)
    import builtins

    real_import = builtins.__import__

    def failing(name, *a, **kw):
        if name == "coordinator_core.ops.sibling_repo_provision_contract":
            raise ImportError("broken contract")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", failing)
    result = _run(repos, monkeypatch)
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == baseline["hookSpecificOutput"]["permissionDecision"]
    assert ROUTE_REMEDY not in _reason(result)
    assert _reason(result) == _reason(baseline)


def test_unmarked_registered_sibling_is_bumped_without_remedy(repos, monkeypatch):
    result = _run(repos, monkeypatch)
    assert result is not None
    assert ROUTE_REMEDY not in _reason(result)
