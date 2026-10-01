"""Pins the worktree_owner verdict table; psutil and the registry are patched."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import psutil
import pytest

from coordinator_core.session import worktree_owner as wo

T0 = 1_790_000_000.0


def _proc(create_time):
    return mock.patch.object(wo.psutil, "Process", return_value=SimpleNamespace(create_time=lambda: create_time))


def _no_proc():
    return mock.patch.object(wo.psutil, "Process", side_effect=psutil.NoSuchProcess(4242))


def _registry(found):
    return mock.patch.object(wo.harness_registry, "record_for_pid", return_value=found)


def _iso(epoch):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_not_locked_is_unattributed():
    out = wo.worktree_owner(False, "")
    assert (out.verdict, out.pid, out.session_id) == ("unattributed", None, None)


@pytest.mark.parametrize(
    "reason",
    ["", "manual hold", "claude agent x (pid abc)", "claude other x (pid 5)", "claude agent x (pid 12345678901)"],
)
def test_non_platform_reason_is_foreign(reason):
    out = wo.worktree_owner(True, reason)
    assert out.verdict == "foreign" and out.pid is None


def test_pid_only_live_with_attribution():
    rec = SimpleNamespace(start_epoch=T0)
    with _proc(T0), _registry(("sess-1", rec)):
        out = wo.worktree_owner(True, "claude agent a1 (pid 4242)")
    assert (out.verdict, out.pid, out.session_id) == ("live", 4242, "sess-1")


def test_pid_only_no_process_is_dead():
    with _no_proc():
        out = wo.worktree_owner(True, "claude session s (pid 4242)")
    assert (out.verdict, out.pid, out.session_id) == ("dead", 4242, None)


def test_start_matching_within_tolerance_is_live():
    with _proc(T0 + 1), _registry(None):
        out = wo.worktree_owner(True, f"claude agent a (pid 4242 start {_iso(T0)})")
    assert out.verdict == "live" and out.session_id is None


def test_start_mismatch_is_recycled_pid_dead():
    with _proc(T0 + 3600), _registry(None):
        out = wo.worktree_owner(True, f"claude agent a (pid 4242 start {_iso(T0)})")
    assert out.verdict == "dead" and out.pid == 4242


def test_unparseable_start_fails_closed():
    with _proc(T0):
        out = wo.worktree_owner(True, "claude agent a (pid 4242 start garbage)")
    assert out.verdict == "foreign"


def test_registry_record_with_other_start_is_not_attributed():
    rec = SimpleNamespace(start_epoch=T0 - 500)
    with _proc(T0), _registry(("old-sess", rec)):
        out = wo.worktree_owner(True, "claude agent a (pid 4242)")
    assert out.verdict == "live" and out.session_id is None


def test_session_id_never_decides_liveness():
    rec = SimpleNamespace(start_epoch=T0)
    with _no_proc(), _registry(("sess-1", rec)):
        out = wo.worktree_owner(True, "claude agent a (pid 4242)")
    assert out.verdict == "dead" and out.session_id is None


def test_exception_fails_closed():
    with mock.patch.object(wo.psutil, "Process", side_effect=RuntimeError("boom")):
        out = wo.worktree_owner(True, "claude agent a (pid 4242)")
    assert out.verdict == "foreign" and "boom" in out.basis


def test_registry_exception_fails_closed():
    with _proc(T0), mock.patch.object(wo.harness_registry, "record_for_pid", side_effect=ValueError("x")):
        out = wo.worktree_owner(True, "claude agent a (pid 4242)")
    assert out.verdict == "foreign"
