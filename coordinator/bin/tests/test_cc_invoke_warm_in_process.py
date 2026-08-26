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

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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


# ---------------------------------------------------------------------------
# AC7 — the import-graph ratchet.
#
# The other AC6/AC7 test above asserts the warm-DISABLED path never binds
# `warm.client`. AC7 additionally pins the warm-ENABLED path's own dependency
# set, on two axes the plan names explicitly:
#
#   (a) importing it registers NO `coordinator_core.ops.*` module -- the
#       guarantee `warm/tests/test_client_does_not_import_op_registry.py`
#       delivers, re-asserted here at this call site because it is the whole
#       of the 2026-08-06 audit's import-cycle objection (plan section "The
#       2026-08-06 ruling that named this surface, and why it does not bind").
#   (b) a ratchet on the COUNT of `coordinator_core.*` modules the helper's
#       import path pulls, against the 19 pinned in `invoke/__main__.py`'s own
#       measured comment. A count, never a timing -- AC7 says so.
#
# Both run in a FRESH interpreter: this test process has already imported
# much of `coordinator_core`, so an in-process `sys.modules` read here would
# measure the test runner, not the helper.
# ---------------------------------------------------------------------------

#: Pinned in `coordinator_core/invoke/__main__.py`'s measured line-comment as
#: the module count `warm.client`'s eager imports pull. A ceiling, not an
#: equality: a DROP is an improvement and must not turn this red. A rise means
#: the helper's import path grew a new dependency -- re-measure before moving.
_AC7_MODULE_CEILING = 19

_AC7_PROBE = """
import sys
sys.path.insert(0, {repo!r})
from coordinator_core.warm.settings import is_warm_enabled
from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS
from coordinator_core.warm.client import try_warm_dispatch
_cc = sorted(m for m in sys.modules if m.startswith("coordinator_core"))
_ops = [m for m in _cc if m.startswith("coordinator_core.ops")]
print(len(_cc))
print("|".join(_ops))
"""


def _run_ac7_probe() -> tuple[int, list[str]]:
    """Import the helper's dependency set in a fresh interpreter.

    Returns `(coordinator_core_module_count, ops_modules_registered)`.
    """
    import subprocess

    repo_root = _BIN_DIR.parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", _AC7_PROBE.format(repo=str(repo_root))],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(
            "AC7 probe could not import the real coordinator_core in this "
            f"environment (rc={proc.returncode}): {proc.stderr[-400:]}"
        )
    lines = proc.stdout.strip().split("\n")
    count = int(lines[0])
    ops = [m for m in (lines[1] if len(lines) > 1 else "").split("|") if m]
    return count, ops


def test_warm_helper_imports_register_no_op_registry_modules():
    """AC7(a): the helper's dependency set pulls in no `coordinator_core.ops.*`.

    This is the 2026-08-06 audit's stated objection to converting this call
    site -- "importing it pulls the whole op registry into every
    coordinator/bin/ CLI". The plan's rebuttal is that it imports
    `warm.client`, not `coordinator_core.invoke`. This test is what makes
    that rebuttal falsifiable rather than an assertion.
    """
    _count, ops = _run_ac7_probe()
    assert ops == [], f"helper import path registered op-registry modules: {ops}"


def test_warm_helper_import_module_count_stays_under_the_pin():
    """AC7(b): a ratchet on the `coordinator_core.*` module count, not a timing."""
    count, _ops = _run_ac7_probe()
    assert count <= _AC7_MODULE_CEILING, (
        f"helper import path now pulls {count} coordinator_core modules, "
        f"above the pinned ceiling of {_AC7_MODULE_CEILING}. A new dependency "
        "entered the warm-reach path -- re-measure and re-justify before "
        "raising this number."
    )
