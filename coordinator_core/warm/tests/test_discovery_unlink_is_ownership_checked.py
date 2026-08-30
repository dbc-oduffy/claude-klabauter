"""A departing listener must not delete the LIVE listener's discovery record.

WHY THIS FILE EXISTS. `supervisor.ctx_shutdown` and `front_door.ctx_shutdown`
both unlinked the discovery file unconditionally. There is exactly ONE
discovery file per clone and every winning listener overwrites it at boot, so
a superseded or orphaned generation exiting deleted the record describing its
LIVE SUCCESSOR. The next `http_hook_forwarder._resolve_backend` then read
`None` and DENIED a PreToolUse call the Bash guard never evaluated.

Measured on this box 2026-08-30, which is why this is a regression test and
not a hypothetical: up to 9 concurrent HTTP listeners, 92 of 131 lifetimes
serving zero requests, and death groups of 4-8 processes inside one second --
every one of those exits deleting the surviving listener's record. Forwarder
ledger over the same window: 168 of 14,174 PreToolUse calls denied.

The two sibling cleanup paths (`breadcrumb.unlink_breadcrumb(owner_pid=...)`
and `election.unlink_if_owned`) already carried this guard, each with a
docstring spelling out this exact failure. These tests pin the discovery
file's own copy of it.

NEGATIVE SPEC -- what these tests deliberately do NOT assert:
  * NOT that `owner_pid=None` refuses. The unconditional path is retained for
    callers that own the file unambiguously, and existing callers rely on it.
  * NOT anything about WHY a listener is departing. Ownership is the only
    question here; skew, supersession and idle demotion are elsewhere.
  * NOT that the unlink succeeds -- these are best-effort, never-raises
    functions, and a failed unlink is not this guard's concern.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.warm import front_door, supervisor


MODULES = pytest.mark.parametrize("mod", [supervisor, front_door], ids=["supervisor", "front_door"])


def _write_record(mod, root, pid: int) -> None:
    path = mod.discovery_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "port": 1234}), encoding="utf-8")


@MODULES
def test_departing_listener_does_not_delete_the_live_successors_record(mod, tmp_path):
    """The measured failure, stated as the sequence that produced it."""
    _write_record(mod, tmp_path, pid=2222)  # successor B booted and overwrote A's

    mod.unlink_discovery(tmp_path, owner_pid=1111)  # A exits

    assert mod.discovery_path(tmp_path).exists(), (
        "a superseded listener's exit deleted the live successor's discovery "
        "record -- the forwarder's next read returns None and denies"
    )
    assert json.loads(mod.discovery_path(tmp_path).read_text(encoding="utf-8"))["pid"] == 2222


@MODULES
def test_owner_removes_its_own_record(mod, tmp_path):
    _write_record(mod, tmp_path, pid=1111)

    mod.unlink_discovery(tmp_path, owner_pid=1111)

    assert not mod.discovery_path(tmp_path).exists()


@MODULES
def test_unconditional_unlink_is_retained_when_no_owner_is_named(mod, tmp_path):
    """Existing callers pass no `owner_pid`; that path must not change."""
    _write_record(mod, tmp_path, pid=2222)

    mod.unlink_discovery(tmp_path)

    assert not mod.discovery_path(tmp_path).exists()


@MODULES
def test_absent_record_is_a_no_op_and_never_raises(mod, tmp_path):
    mod.unlink_discovery(tmp_path, owner_pid=1111)  # must not raise


@MODULES
def test_unreadable_record_does_not_delete(mod, tmp_path):
    """`read_discovery` returns None for malformed JSON. A record we cannot
    vouch for is one we must not delete -- same fail-safe direction as
    `discovery_is_live`'s "cannot vouch" -> False."""
    path = mod.discovery_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    mod.unlink_discovery(tmp_path, owner_pid=1111)

    assert path.exists()
