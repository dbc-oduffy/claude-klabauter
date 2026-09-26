"""coordinator/bin/tests/test_assembler_warm_route_cold_fallback.py -- C4's
own unit coverage for `entry_point_shim.py :: _native_route_entry`, the
`invoke.from_argv` `params.entrypoint` route for `pickup-assemble`,
`baton-assemble`, and `workstream-complete-assemble`.

Spec backlink: docs/plans/2026-09-06-three-assembler-briefs-under-the-
brightline.md, chunk C4.

Scope: the ROUTING code this chunk owns, isolated from the real warm
transport (`cc_invoke.route` is monkeypatched per-scenario) and from the
real CLAUDE_KLABAUTER_ROOT registry (`entry_point_shim._import_engine_module` is
monkeypatched to a bare `importlib.import_module` against this repo's own
tree), same isolation convention as
`test_entry_point_shim_warm_route.py`.

Negative-spec:
    - Does NOT exercise the real `coordinator_core.invoke` warm transport
      (UDS socket, JSON-RPC serialization) -- that is `cc_invoke`'s own
      test surface.
    - Does NOT re-test `route()`'s own State-1/State-2 gate logic --
      `route` is stubbed to mirror the two states' documented contract
      (State-1: call `legacy_fn()`; State-2 transport failure: raise),
      never re-derived here (C4's row: "route()'s own contract ... not
      re-implemented here").
    - Does NOT assert a State-2 fallback. `route`'s own docstring: "NEVER
      fall back to legacy_fn on State-2" -- a test that pinned a fallback
      there would pin a defect the router forbids.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_DIR = _REPO_ROOT / "coordinator" / "bin" / "lib"

for _p in (str(_LIB_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entry_point_shim  # noqa: E402
import cc_invoke  # noqa: E402

# name -> dotted engine module, per `entry_point_shim._ENGINE_ENTRIES`'s own
_ROUTED_TARGETS = {
    "pickup-assemble": "coordinator_core.pickup_brief",
    "baton-assemble": "coordinator_core.baton_assemble",
    "workstream-complete-assemble": "coordinator_core.workstream_complete",
}


@pytest.fixture(autouse=True)
def _decouple_from_real_engine_root(monkeypatch):
    monkeypatch.setattr(
        entry_point_shim,
        "_import_engine_module",
        lambda dotted: importlib.import_module(dotted),
    )


def _stub_route(result=None, exc=None):

    def _route(op, params, repo_root, legacy_fn, **kwargs):
        if exc is not None:
            raise exc
        return result

    return _route


def _seam_absent_route(op, params, repo_root, legacy_fn, **kwargs):
    return legacy_fn()


@pytest.mark.parametrize("name, dotted", list(_ROUTED_TARGETS.items()))
def test_route_call_carries_this_name_as_entrypoint(monkeypatch, name, dotted):
    entry = entry_point_shim._ENGINE_ENTRIES[name]
    seen = {}

    def _capture_route(op, params, repo_root, legacy_fn, **kwargs):
        seen["op"] = op
        seen["params"] = params
        return {"stdout": "", "stderr": "", "exit_code": 0}

    monkeypatch.setattr(cc_invoke, "route", _capture_route)
    code = entry(["--bogus-noop-argv"])

    assert code == 0
    assert seen["op"] == "invoke.from_argv"
    assert seen["params"]["entrypoint"] == name
    assert seen["params"]["argv"] == ["--bogus-noop-argv"]
    assert isinstance(seen["params"]["cwd"], str) and seen["params"]["cwd"]


@pytest.mark.parametrize("name, dotted", list(_ROUTED_TARGETS.items()))
def test_state1_seam_absent_falls_back_to_legacy_entry(monkeypatch, name, dotted):
    entry = entry_point_shim._ENGINE_ENTRIES[name]
    legacy_calls = []

    def _fake_legacy(argv):
        legacy_calls.append(list(argv))
        return 7

    monkeypatch.setattr(
        entry_point_shim,
        "_simple_entry",
        lambda n, d: _fake_legacy,
    )
    # construction time, so patching `_simple_entry` after `_ENGINE_ENTRIES`
    routed = entry_point_shim._native_route_entry(name, dotted)
    monkeypatch.setattr(cc_invoke, "route", _seam_absent_route)

    code = routed(["apply"])

    assert code == 7
    assert legacy_calls == [["apply"]]


def test_state1_legacy_entry_is_byte_identical_simple_entry(monkeypatch, capsys):
    name, dotted = "pickup-assemble", "coordinator_core.pickup_brief"
    monkeypatch.setattr(
        entry_point_shim,
        "_import_engine_module",
        lambda d: (_ for _ in ()).throw(ImportError("boom")),
    )
    monkeypatch.setattr(cc_invoke, "route", _seam_absent_route)

    routed = entry_point_shim._native_route_entry(name, dotted)
    code = routed(["apply"])

    err = capsys.readouterr().err
    assert code == entry_point_shim._TRANSPORT_FAIL
    assert f"{name}: {dotted} not importable" in err


@pytest.mark.parametrize("name, dotted", list(_ROUTED_TARGETS.items()))
def test_state2_native_result_prints_and_returns_exit_code(monkeypatch, name, dotted, capsys):
    entry = entry_point_shim._ENGINE_ENTRIES[name]
    monkeypatch.setattr(
        cc_invoke,
        "route",
        _stub_route(result={"stdout": "native stdout\n", "stderr": "native stderr\n", "exit_code": 2}),
    )

    code = entry(["apply"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == "native stdout\n"
    assert captured.err == "native stderr\n"


@pytest.mark.parametrize("name, dotted", list(_ROUTED_TARGETS.items()))
def test_state2_non_int_exit_code_defaults_to_one(monkeypatch, name, dotted):
    entry = entry_point_shim._ENGINE_ENTRIES[name]
    monkeypatch.setattr(
        cc_invoke,
        "route",
        _stub_route(result={"stdout": "", "stderr": "", "exit_code": None}),
    )

    assert entry(["apply"]) == 1


@pytest.mark.parametrize("name, dotted", list(_ROUTED_TARGETS.items()))
def test_state2_transport_failure_raises_hard_never_falls_back(monkeypatch, name, dotted):
    entry = entry_point_shim._ENGINE_ENTRIES[name]
    monkeypatch.setattr(
        cc_invoke,
        "route",
        _stub_route(exc=RuntimeError("connection reset after send")),
    )

    with pytest.raises(RuntimeError, match="connection reset after send"):
        entry(["apply"])
