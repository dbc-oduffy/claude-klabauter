from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _install_fake_module(monkeypatch, name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_warm_enabled_hit_returns_envelope(monkeypatch):
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
    monkeypatch.delitem(sys.modules, "coordinator_core.warm.client", raising=False)

    _install_fake_module(
        monkeypatch, "coordinator_core.warm.settings", is_warm_enabled=lambda: False
    )

    def _fail_if_imported(*args, **kwargs):
        raise AssertionError("coordinator_core.warm.client must not be imported when warm is disabled")

    _install_fake_module(
        monkeypatch, "coordinator_core.op_scopes", WORKTREE_SCOPED_OPS=frozenset()
    )
    warm_client_guard = types.ModuleType("coordinator_core.warm.client")
    warm_client_guard.__getattr__ = _fail_if_imported  # type: ignore[attr-defined]

    result = _mod._try_in_process_warm_reach("some.op", {}, "/repo/root")

    assert result is None
    assert "coordinator_core.warm.client" not in sys.modules


def test_worktree_scoped_op_carries_origin_worktree(monkeypatch):
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


# The other AC6/AC7 test above asserts the warm-DISABLED path never binds

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
    _count, ops = _run_ac7_probe()
    assert ops == [], f"helper import path registered op-registry modules: {ops}"


def test_warm_helper_import_module_count_stays_under_the_pin():
    count, _ops = _run_ac7_probe()
    assert count <= _AC7_MODULE_CEILING, (
        f"helper import path now pulls {count} coordinator_core modules, "
        f"above the pinned ceiling of {_AC7_MODULE_CEILING}. A new dependency "
        "entered the warm-reach path -- re-measure and re-justify before "
        "raising this number."
    )
