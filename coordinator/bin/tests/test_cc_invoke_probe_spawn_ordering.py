"""test_cc_invoke_probe_spawn_ordering.py — the op-timeout probe never runs on a warm hit.

C3 (state/dispatch-briefs/2026-08-26-the-op-clis-dial-warm-from-the-process/C3.md):
`_op_timeout_ceiling` (and through it `_resolve_op_timeouts`, the op-budget-dump
spawn) exists to bound a SUBPROCESS wait. On a warm hit there is no subprocess,
so the ceiling must not be computed and the probe must not run. Both `cc_invoke()`
and `cc_invoke_bare()` call `_op_timeout_ceiling` only in the cold-spawn block
that follows a warm-miss (`_capture_warm_reach` returning `None`) — this module
asserts that on a live warm hit, `_resolve_op_timeouts` is never entered.

Asserted on module state (a patched sentinel that raises if called), never on
wall time — see this repo's own doctrine against timing-based assertions.

Run: pytest coordinator/bin/tests/test_cc_invoke_probe_spawn_ordering.py -v
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_cc_invoke_warm_in_process.py's own layout.
# test file: coordinator/bin/tests/test_cc_invoke_probe_spawn_ordering.py
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

    Mirrors test_cc_invoke_warm_in_process.py's own helper of the same name.
    """
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _install_warm_hit(monkeypatch, result: dict[str, Any]) -> None:
    """Wire `coordinator_core.warm.{settings,client}` so a warm reach is a hit
    returning `{"jsonrpc": "2.0", "id": 1, "result": result}`."""

    def _fake_try_warm_dispatch(msg):
        return {"jsonrpc": "2.0", "id": 1, "result": result}

    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: True
    )
    _install_fake_module(
        monkeypatch, "coordinator_core.op_scopes", WORKTREE_SCOPED_OPS=frozenset()
    )
    _install_fake_module(
        monkeypatch, "coordinator_core.warm.client", try_warm_dispatch=_fake_try_warm_dispatch
    )


def _forbid_resolve_op_timeouts(monkeypatch) -> None:
    """Patch `_resolve_op_timeouts` to fail loud if the cold-spawn's own
    ceiling computation is ever reached on a warm hit."""

    def _fail(*args, **kwargs):
        raise AssertionError(
            "_resolve_op_timeouts must not be entered on a warm hit — the "
            "op-timeout probe bounds a subprocess wait, and a warm hit has none"
        )

    monkeypatch.setattr(_mod, "_resolve_op_timeouts", _fail)


def test_cc_invoke_warm_hit_never_enters_resolve_op_timeouts(monkeypatch):
    """A live warm hit through `cc_invoke()` returns the served result without
    ever calling `_resolve_op_timeouts` (and therefore never computing
    `_op_timeout_ceiling`)."""
    _install_warm_hit(monkeypatch, {"ok": True})
    _forbid_resolve_op_timeouts(monkeypatch)

    result = _mod.cc_invoke("some.op", {}, "/repo/root", _claude_klabauter_root="/fake/engine/root")

    assert result == {"ok": True}


def test_cc_invoke_bare_warm_hit_never_enters_resolve_op_timeouts(monkeypatch):
    """Same guarantee for `cc_invoke_bare()`."""
    _install_warm_hit(monkeypatch, {"ok": True})
    _forbid_resolve_op_timeouts(monkeypatch)

    result = _mod.cc_invoke_bare("some.op", {}, "/repo/root", _claude_klabauter_root="/fake/engine/root")

    assert result == {"ok": True}


def test_cc_invoke_cold_path_still_computes_ceiling_on_a_miss(monkeypatch):
    """Sanity check on the other side: a warm MISS still reaches
    `_op_timeout_ceiling` (proves the forbid-helper above is a real probe, not
    a vacuous pass) — asserted via a sentinel append, not wall time."""
    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: False
    )
    monkeypatch.delitem(sys.modules, "coordinator_core.warm.client", raising=False)

    entered: list[bool] = []

    def _sentinel(*args, **kwargs):
        entered.append(True)
        raise RuntimeError("stop before an actual subprocess spawn")

    monkeypatch.setattr(_mod, "_op_timeout_ceiling", _sentinel)

    with pytest.raises(RuntimeError, match="stop before an actual subprocess spawn"):
        _mod.cc_invoke("some.op", {}, "/repo/root", _claude_klabauter_root="/fake/engine/root")

    assert entered == [True]
