"""
coordinator_core.tests.test_dispatch — Dispatch-layer routing tests for the three
reclassified emit ops (artifact.emit, backlog.record, goal.append).

Plan § C3 deliverable: dispatch tests assert emit ops receive a non-None repo_root
derived from _origin_worktree, and fail-loud (INVALID_PARAMS -32602) when absent.

These tests exercise ipc.dispatch_message (the routing/keying layer), NOT the handler
internals. Handler-level guard tests (None raises ValueError; resolve_context called with
main_worktree_root) live in test_c4a_handler_wiring.py and test_recorder.py.

Review: code-reviewer (S2-F1/F2/F3 + S4-F2) — plan C3 deliverable: dispatch tests asserting
the 3 reclassified emit ops receive non-None repo_root when _origin_worktree is present and
return INVALID_PARAMS when absent. Neither path was covered before this file.

Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C3 / AC1
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ipc import (
    INVALID_PARAMS,
    _ORIGIN_WORKTREE_FIELD,
    _REGISTRY,
    dispatch_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency."""
    return asyncio.run(coro)


class _RegistryScope:
    """Context manager: install test handlers into _REGISTRY, restore on exit.

    Saves/restores only the keys it touches so test-local ops do not bleed
    into the real registry or across tests.
    """

    def __init__(self, handlers: dict) -> None:
        self._handlers = handlers
        self._saved: dict = {}

    def __enter__(self):
        for name in self._handlers:
            self._saved[name] = _REGISTRY.get(name)
        _REGISTRY.update(self._handlers)
        return self

    def __exit__(self, *_):
        for name, old in self._saved.items():
            if old is None:
                _REGISTRY.pop(name, None)
            else:
                _REGISTRY[name] = old


# ---------------------------------------------------------------------------
# emit ops receive non-None repo_root when _origin_worktree present
# ---------------------------------------------------------------------------

class TestEmitOpsReceiveRepoRootFromOriginWorktree:
    """Dispatch layer correctly injects a non-None repo_root into the 3 reclassified emit ops.

    Exercises the "common_dir" keying path in resolve_op_repo_key: when _origin_worktree is
    present in the message, git_common_dir resolves it, and dispatch_message calls the handler
    with repo_root = the resolved common_dir path (non-None).

    The real handlers are replaced with spies to isolate the dispatch-layer wiring test from
    handler internals (the handler-level tests live in test_c4a_handler_wiring.py / test_recorder.py).
    """

    def _dispatch_with_spy(self, method: str, fake_common_dir: Path) -> Path | None:
        """Dispatch method with mocked git_common_dir; return the repo_root captured by the spy."""
        captured: list[Path | None] = []

        async def _spy_handler(params: dict, repo_root=None) -> dict:
            captured.append(repo_root)
            return {"ok": True}

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {},
            _ORIGIN_WORKTREE_FIELD: str(fake_common_dir.parent),  # a valid worktree path
        }
        with patch("coordinator_core.ipc.git_common_dir", return_value=fake_common_dir), \
             _RegistryScope({method: _spy_handler}):
            _run(dispatch_message(msg))

        return captured[0] if captured else None

    def test_artifact_emit_receives_non_none_repo_root(self, tmp_path: Path) -> None:
        """artifact.emit handler receives non-None repo_root when _origin_worktree is present.

        Regression guard: if artifact.emit were accidentally reclassified back to "central",
        resolve_op_repo_key would return None and the handler would receive repo_root=None,
        reintroducing the hardlocked-to-~/.claude bug (2026-07-07 per-repo emission cutover).
        """
        fake_common_dir = tmp_path / ".git"
        received = self._dispatch_with_spy("artifact.emit", fake_common_dir)
        assert received is not None, (
            "artifact.emit handler must receive non-None repo_root when _origin_worktree present; "
            "None means the op was silently reclassified to 'central' scope (regression)"
        )
        assert received == fake_common_dir

    def test_backlog_record_receives_non_none_repo_root(self, tmp_path: Path) -> None:
        """backlog.record handler receives non-None repo_root when _origin_worktree is present."""
        fake_common_dir = tmp_path / ".git"
        received = self._dispatch_with_spy("backlog.record", fake_common_dir)
        assert received is not None, (
            "backlog.record handler must receive non-None repo_root when _origin_worktree present"
        )
        assert received == fake_common_dir

    def test_goal_append_receives_non_none_repo_root(self, tmp_path: Path) -> None:
        """goal.append handler receives non-None repo_root when _origin_worktree is present."""
        fake_common_dir = tmp_path / ".git"
        received = self._dispatch_with_spy("goal.append", fake_common_dir)
        assert received is not None, (
            "goal.append handler must receive non-None repo_root when _origin_worktree present"
        )
        assert received == fake_common_dir

    def test_emit_cadence_receives_non_none_repo_root(self, tmp_path: Path) -> None:
        """emit.cadence handler receives non-None repo_root when _origin_worktree is present."""
        fake_common_dir = tmp_path / ".git"
        received = self._dispatch_with_spy("emit.cadence", fake_common_dir)
        assert received is not None, (
            "emit.cadence handler must receive non-None repo_root when _origin_worktree present"
        )
        assert received == fake_common_dir


# ---------------------------------------------------------------------------
# emit ops return INVALID_PARAMS (-32602) when _origin_worktree absent
# ---------------------------------------------------------------------------

class TestEmitOpsFailLoudWithoutOriginWorktree:
    """Dispatch layer returns INVALID_PARAMS (-32602) when _origin_worktree is absent.

    The 3 reclassified emit ops are "common_dir"-scoped. resolve_op_repo_key raises
    ValueError when request_repo is None (no _origin_worktree in message), which
    dispatch_message converts to INVALID_PARAMS. This is the dispatch-layer fail-loud
    gate (AC1 / AC5 line of defense before the handler's own None guard).

    These tests do NOT replace the handler-level tests (test_c4a_handler_wiring.py /
    test_recorder.py TestBacklogRecordHandler) — they test the dispatch layer independently.
    """

    @staticmethod
    def _msg_without_worktree(method: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {},
            # _origin_worktree deliberately absent
        }

    def test_artifact_emit_without_origin_worktree_returns_invalid_params(self) -> None:
        """artifact.emit without _origin_worktree → error code -32602 (INVALID_PARAMS)."""
        d = _run(dispatch_message(self._msg_without_worktree("artifact.emit")))
        assert "error" in d, "Expected error response when _origin_worktree is absent"
        assert d["error"]["code"] == INVALID_PARAMS, (
            f"Expected INVALID_PARAMS ({INVALID_PARAMS}); got {d['error']['code']}"
        )

    def test_backlog_record_without_origin_worktree_returns_invalid_params(self) -> None:
        """backlog.record without _origin_worktree → error code -32602 (INVALID_PARAMS)."""
        d = _run(dispatch_message(self._msg_without_worktree("backlog.record")))
        assert "error" in d
        assert d["error"]["code"] == INVALID_PARAMS

    def test_goal_append_without_origin_worktree_returns_invalid_params(self) -> None:
        """goal.append without _origin_worktree → error code -32602 (INVALID_PARAMS)."""
        d = _run(dispatch_message(self._msg_without_worktree("goal.append")))
        assert "error" in d
        assert d["error"]["code"] == INVALID_PARAMS

    def test_emit_cadence_without_origin_worktree_returns_invalid_params(self) -> None:
        """emit.cadence without _origin_worktree → error code -32602 (INVALID_PARAMS)."""
        d = _run(dispatch_message(self._msg_without_worktree("emit.cadence")))
        assert "error" in d
        assert d["error"]["code"] == INVALID_PARAMS
