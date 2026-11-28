"""Incident pin: a refire under the resume launcher never starts a script holding a `workflow()` stage reader.

The refire hook leaves the halted run holding a live token, so the launcher child's `dispatch.emit` of that run is
answered as a resend with the run's existing script. A script emitted without `launcher_child` holds a
`workflow()` stage reader, which the launcher's own `workflow()` start puts at a second nesting level; the runtime
refuses it and the run halts with "stage reply sidecar does not match its handle". The child emit of such a run
carries `reserved_run_id`, so the emit composes the reader-free launcher-child script and the launcher starts that.
Drives the production refire hook, then the production emit with the params the launcher child sends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    REL,
    WARP,
    _halt,
    composed,
    repo,
)

pytestmark = pytest.mark.complete

_SESSION = "11111111-2222-3333-4444-555555555555"


def _refire_children(repo: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The children the refire hook hands the launcher for the halted sizing, through `evaluate`."""
    monkeypatch.setattr(hook, "_spawn_recompose", lambda root, run_id: True)
    launcher = repo / "launcher.js"
    launcher.write_text("//", encoding="utf-8")
    seen: list[Any] = []

    def launch(root, parent_id, children, session_id):
        seen.extend(children)
        return launcher

    payload = {
        "tool_name": "Workflow", "cwd": str(repo), "session_id": _SESSION, "tool_use_id": "toolu_refire",
        "tool_input": {"name": "coordinator:engine-ask", "args": {"sizing": REL}},
    }
    env = hook.evaluate(payload, emit=lambda p, r: pytest.fail("plain emit"), launch=launch)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert [c.run_id for c in seen] == [WARP], seen
    return seen


def _launcher_child_emit(repo: Path, child) -> dict:
    """The emit the launcher child's relay sends for `child`: its params with the launcher flag the deferred script adds."""
    return op._dispatch_emit({**child.emit_params, LAUNCHER_CHILD_EMIT_PARAM: True}, repo_root=repo)


def test_a_refired_run_holding_a_stage_reader_is_emitted_reader_free_for_the_launcher(repo, composed, monkeypatch):
    _halt(repo, WARP, "a" * 40)
    first = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=repo)
    assert first["run_id"] == WARP
    assert "workflow(" in Path(first["path"]).read_text(encoding="utf-8"), "the first emit must hold its own stage reader"
    _halt(repo, WARP, "a" * 40)

    (child,) = _refire_children(repo, monkeypatch)
    assert child.emit_params["reserved_run_id"] == WARP

    reply = _launcher_child_emit(repo, child)
    assert reply["run_id"] == WARP and not reply.get("deduped"), reply
    assert "workflow(" not in Path(reply["path"]).read_text(encoding="utf-8")


def test_a_refired_run_with_no_stage_reader_keeps_the_resend_and_adds_no_reservation(repo, composed, monkeypatch):
    _halt(repo, WARP, "a" * 40)
    first = op._dispatch_emit({"ask": True, "sizing_path": REL, LAUNCHER_CHILD_EMIT_PARAM: True}, repo_root=repo)
    assert "workflow(" not in Path(first["path"]).read_text(encoding="utf-8")
    _halt(repo, WARP, "a" * 40)

    (child,) = _refire_children(repo, monkeypatch)
    assert "reserved_run_id" not in child.emit_params
