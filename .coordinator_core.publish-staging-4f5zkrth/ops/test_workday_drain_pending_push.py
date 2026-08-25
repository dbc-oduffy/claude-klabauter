"""
Tests for coordinator_core.ops.workday_drain_pending_push —
AC14's workday-start push-health drain point (workday.drain_pending_push).

Covers: the missing-repo_root premise failure, op registration + handler
param-authoritativeness (mirroring workday.surface_auto_push_failure_stats'
own test shape), that the handler delegates verbatim to
auto_push.drain_pending_push (never re-implements the drain), and an
end-to-end no-op-when-no-record case proving this op never invents a push
where none is due.
"""

from __future__ import annotations

import pytest

from coordinator_core.hooks import auto_push
from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import workday_drain_pending_push as mod


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def test_missing_repo_root_raises_structured_error():
    with pytest.raises(mod.DrainPendingPushError, match="repo_root"):
        mod._handler({}, None)


def test_op_registered_and_handler_requires_param(repo):
    handler = get_op_handler("workday.drain_pending_push")
    assert handler is not None
    with pytest.raises(mod.DrainPendingPushError, match="repo_root"):
        handler({}, None)
    result = handler({"repo_root": str(repo)}, None)
    assert result == {"drained": True}


def test_handler_delegates_to_auto_push_drain_pending_push_verbatim(monkeypatch, repo):
    calls = []
    monkeypatch.setattr(
        auto_push, "drain_pending_push", lambda root: calls.append(root)
    )
    # mod imported drain_pending_push by reference at module load time —
    # patch the name this module actually calls, not just auto_push's own.
    monkeypatch.setattr(mod, "drain_pending_push", lambda root: calls.append(root))

    result = mod._handler({"repo_root": str(repo)}, None)

    assert calls == [str(repo)]
    assert result == {"drained": True}


def test_ipc_injected_repo_root_kwarg_is_ignored_explicit_param_wins(monkeypatch, repo):
    calls = []
    monkeypatch.setattr(mod, "drain_pending_push", lambda root: calls.append(root))

    mod._handler({"repo_root": str(repo)}, repo_root="/some/other/injected/path")

    assert calls == [str(repo)]


def test_no_pending_record_is_a_true_no_op(repo):
    """End-to-end (real drain_pending_push, no monkeypatch): an absent
    pending record must never invent a push — drained:true reflects only
    that the call ran, not that anything was due."""
    assert not (repo / ".git" / auto_push._PENDING_RECORD_NAME).exists()
    result = mod._handler({"repo_root": str(repo)}, None)
    assert result == {"drained": True}
    assert not (repo / ".git" / auto_push._PENDING_RECORD_NAME).exists()
