"""
coordinator_core.tests.test_dispatch — Dispatch-layer routing tests for the
reclassified per-repo state writer (goal.append).

Plan § C3 deliverable: dispatch tests assert emit ops receive a non-None repo_root
derived from _origin_worktree, and fail-loud (INVALID_PARAMS -32602) when absent.

These tests exercise ipc.dispatch_message (the routing/keying layer), NOT the handler
internals. Handler-level guard tests (None raises ValueError; resolve_context called with
main_worktree_root) live in test_c4a_handler_wiring.py.

Plan C3 deliverable: dispatch tests asserting
the reclassified emit ops receive non-None repo_root when _origin_worktree is present and
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


def _run(coro):
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


class TestEmitOpsReceiveRepoRootFromOriginWorktree:

    def _dispatch_with_spy(self, method: str, fake_common_dir: Path) -> Path | None:
        captured: list[Path | None] = []

        async def _spy_handler(params: dict, repo_root=None) -> dict:
            captured.append(repo_root)
            return {"ok": True}

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {},
            _ORIGIN_WORKTREE_FIELD: str(fake_common_dir.parent),
        }
        with patch("coordinator_core.ipc.git_common_dir", return_value=fake_common_dir), \
             _RegistryScope({method: _spy_handler}):
            _run(dispatch_message(msg))

        return captured[0] if captured else None

    def test_goal_append_receives_non_none_repo_root(self, tmp_path: Path) -> None:
        fake_common_dir = tmp_path / ".git"
        received = self._dispatch_with_spy("goal.append", fake_common_dir)
        assert received is not None, (
            "goal.append handler must receive non-None repo_root when _origin_worktree present"
        )
        assert received == fake_common_dir


# emit ops return INVALID_PARAMS (-32602) when _origin_worktree absent

class TestEmitOpsFailLoudWithoutOriginWorktree:
    """Dispatch layer returns INVALID_PARAMS (-32602) when _origin_worktree is absent.

    The reclassified per-repo state writers are "common_dir"-scoped. resolve_op_repo_key raises
    ValueError when request_repo is None (no _origin_worktree in message), which
    dispatch_message converts to INVALID_PARAMS. This is the dispatch-layer fail-loud
    gate (AC1 / AC5 line of defense before the handler's own None guard).

    These tests do NOT replace the handler-level tests — they test the dispatch layer
    independently.
    """

    @staticmethod
    def _msg_without_worktree(method: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {},
        }

    def test_goal_append_without_origin_worktree_returns_invalid_params(self) -> None:
        """goal.append without _origin_worktree → error code -32602 (INVALID_PARAMS)."""
        d = _run(dispatch_message(self._msg_without_worktree("goal.append")))
        assert "error" in d
        assert d["error"]["code"] == INVALID_PARAMS
