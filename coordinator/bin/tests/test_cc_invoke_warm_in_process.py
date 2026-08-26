"""test_cc_invoke_warm_in_process.py — unit tests for `cc_invoke._try_in_process_warm_reach`.

C1 (state/dispatch-briefs/2026-08-26-the-op-clis-dial-warm-from-the-process/C1.md):
a shared in-process warm-reach helper in `cc_invoke.py`, gated on
`is_warm_enabled`, for `cc_invoke()`/`cc_invoke_bare()` to call before paying
a cold subprocess spawn.

Tests:
  - warm-enabled hit returns the envelope `try_warm_dispatch` produced
  - warm-disabled returns None (and `warm.client` is never imported —
    AC6/AC7, asserted by import-graph inspection, not timing)
  - a worktree-scoped op's built msg carries `_origin_worktree == repo_root`
  - a none-scoped op's built msg carries no `_origin_worktree` key

Run: pytest coordinator/bin/tests/test_cc_invoke_warm_in_process.py -v
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_cc_invoke_py.py's own layout.
# test file: coordinator/bin/tests/test_cc_invoke_warm_in_process.py
# module:    coordinator/bin/lib/cc_invoke.py
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = pytest.mark.cadence


def _install_fake_module(monkeypatch, name: str, **attrs: Any) -> types.ModuleType:
    """Install a fake module under `sys.modules[name]`, restored by monkeypatch.

    Used to stand in for `coordinator_core.warm.settings`,
    `coordinator_core.op_scopes`, and `coordinator_core.warm.client` without
    requiring the real `coordinator_core` package to be importable in this
    test process's ambient sys.path.
    """
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_warm_enabled_hit_returns_envelope(monkeypatch):
    """A warm hit returns exactly the dict `try_warm_dispatch` produced."""
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    captured_msg: dict[str, Any] = {}

    def _fake_try_warm_dispatch(msg):
        captured_msg.update(msg)
        return envelope

    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: True
    )
    _install_fake_module(
        monkeypatch, "coordinator_core.op_scopes", WORKTREE_SCOPED_OPS=frozenset({"some.op"})
    )
    _install_fake_module(
        monkeypatch, "coordinator_core.warm.client", try_warm_dispatch=_fake_try_warm_dispatch
    )

    result = _mod._try_in_process_warm_reach("some.op", {"a": 1}, "/repo/root")

    assert result is envelope
    assert captured_msg["method"] == "some.op"
    assert captured_msg["params"] == {"a": 1}
    assert captured_msg["jsonrpc"] == "2.0"


def test_warm_disabled_returns_none_and_never_imports_warm_client(monkeypatch):
    """warm-disabled returns None; `warm.client` is never imported (AC6/AC7)."""
    monkeypatch.delitem(sys.modules, "coordinator_core.warm.client", raising=False)

    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: False
    )

    def _fail_if_imported(*args, **kwargs):
        raise AssertionError("coordinator_core.warm.client must not be imported when warm is disabled")

    _install_fake_module(
        monkeypatch, "coordinator_core.op_scopes", WORKTREE_SCOPED_OPS=frozenset()
    )
    # A module object whose __getattr__ fails loud stands in for "imported ==
    # test failure": if `_try_in_process_warm_reach` reaches for
    # `try_warm_dispatch` on this module, the AttributeError below fails the
    # test just as surely as the explicit sys.modules absence-check.
    warm_client_guard = types.ModuleType("coordinator_core.warm.client")
    warm_client_guard.__getattr__ = _fail_if_imported  # type: ignore[attr-defined]

    result = _mod._try_in_process_warm_reach("some.op", {}, "/repo/root")

    assert result is None
    # AC6/AC7: import-graph inspection — the disabled-warm path never binds
    # coordinator_core.warm.client into sys.modules at all.
    assert "coordinator_core.warm.client" not in sys.modules


def test_worktree_scoped_op_carries_origin_worktree(monkeypatch):
    """A worktree-scoped op's built msg carries `_origin_worktree == repo_root`."""
    captured_msg: dict[str, Any] = {}

    def _fake_try_warm_dispatch(msg):
        captured_msg.update(msg)
        return {"result": {}}

    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: True
    )
    _install_fake_module(
        monkeypatch,
        "coordinator_core.op_scopes",
        WORKTREE_SCOPED_OPS=frozenset({"scoped.op"}),
    )
    _install_fake_module(
        monkeypatch, "coordinator_core.warm.client", try_warm_dispatch=_fake_try_warm_dispatch
    )

    _mod._try_in_process_warm_reach("scoped.op", {}, "/repo/root")

    assert captured_msg["_origin_worktree"] == "/repo/root"
    assert "_caller_cwd" in captured_msg


def test_none_scoped_op_omits_origin_worktree(monkeypatch):
    """A none-scoped op's built msg carries no `_origin_worktree` key."""
    captured_msg: dict[str, Any] = {}

    def _fake_try_warm_dispatch(msg):
        captured_msg.update(msg)
        return {"result": {}}

    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: True
    )
    _install_fake_module(
        monkeypatch,
        "coordinator_core.op_scopes",
        WORKTREE_SCOPED_OPS=frozenset({"scoped.op"}),
    )
    _install_fake_module(
        monkeypatch, "coordinator_core.warm.client", try_warm_dispatch=_fake_try_warm_dispatch
    )

    _mod._try_in_process_warm_reach("none.scoped.op", {}, "/repo/root")

    assert "_origin_worktree" not in captured_msg
    assert "_caller_cwd" in captured_msg
