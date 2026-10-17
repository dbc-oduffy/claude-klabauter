"""archive_stamp keeps asyncio off its import path and routes stamp/transition through the sync cores."""

from __future__ import annotations

from unittest import mock

import pytest

def test_module_has_no_module_level_asyncio_name():
    from coordinator_core import archive_stamp

    assert not hasattr(archive_stamp, "asyncio")


def test_transition_wrapper_calls_transition_sync(tmp_path):
    from coordinator_core import archive_stamp

    h = tmp_path / "state" / "handoffs" / "x.md"
    h.parent.mkdir(parents=True)
    h.write_text("---\nstatus: open\n---\n", encoding="utf-8")
    with mock.patch.object(
        archive_stamp, "_resolve_repo_root_for", return_value=(tmp_path, tmp_path / ".git")
    ), mock.patch(
        "coordinator_core.ops.handoff_transition.transition_sync",
        return_value={"exit_code": 0},
    ) as sync:
        out = archive_stamp._call_handoff_transition(str(h), {"verb": "repark"})
    assert out == {"exit_code": 0}
    assert sync.call_args.args[0]["verb"] == "repark"


def test_stamp_shipped_in_uses_stamp_sync(tmp_path):
    from coordinator_core import archive_stamp

    h = tmp_path / "state" / "handoffs" / "x.md"
    h.parent.mkdir(parents=True)
    h.write_text("---\nstatus: open\n---\n", encoding="utf-8")
    with mock.patch.object(
        archive_stamp, "_resolve_repo_root_for", return_value=(tmp_path, tmp_path / ".git")
    ), mock.patch(
        "coordinator_core.ops.handoff_stamp.stamp_sync",
        return_value={"exit_code": 0, "applied": True},
    ) as sync:
        out = archive_stamp.stamp_shipped_in(
            str(h), kind="ship-commit", sha="abcdef1234567"
        )
    assert out.exit_code == 0 and out.applied
    assert sync.call_args.args[0]["sha"] == "abcdef12"
