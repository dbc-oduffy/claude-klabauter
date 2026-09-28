"""
coordinator_core.ops.tests.test_invoke_from_argv_load_carve_out

C3 of docs/plans/2026-09-27-load-aware-workflow-admission.md: `_run_entrypoint`
refuses a loaded, emission-shaped `emit-dispatch-workflow` call with
`EntrypointNotWarmLoadableError` (-32007) before anything is imported or
`main` is invoked, and leaves every other shape (idle box, `--restamp`,
`--help`, unconfigured, another entrypoint) served warm unchanged.

Stubs `coordinator_core.ops.dispatch_emit.admission` through
`monkeypatch.setitem(sys.modules, ...)` (the module is imported lazily
inside `_run_entrypoint`'s load-conditional branch, precisely so this swap
is observed) so these tests pass independently of C2's real file -- they
pin `_run_entrypoint`'s CALL CONTRACT against the pinned `admission`
contract in the plan's § Design, not C2's implementation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.ops import invoke_from_argv
from coordinator_core.ops.invoke_from_argv import _invoke_from_argv

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])

_ADMISSION_MODULE_NAME = "coordinator_core.ops.dispatch_emit.admission"


class _FakeAdmissionModule:
    """Records every `should_refuse_warm` call; returns a scripted record."""

    def __init__(self, refusal=None):
        self.refusal = refusal
        self.calls = []

    def should_refuse_warm(self, start_dir, *, read_load=None):
        self.calls.append(start_dir)
        return self.refusal


def _install_fake_admission(monkeypatch, refusal=None):
    """Swap `admission` for a fake. `invoke_from_argv`'s lazy `from
    coordinator_core.ops.dispatch_emit import admission` resolves via
    getattr on the already-imported parent package before it consults
    `sys.modules` -- once another test module has imported the real
    `admission` once, that getattr succeeds and a `sys.modules`-only
    swap is silently ignored. Patch both so the fake is observed
    regardless of import order across the suite."""
    fake = _FakeAdmissionModule(refusal)
    monkeypatch.setitem(sys.modules, _ADMISSION_MODULE_NAME, fake)
    import coordinator_core.ops.dispatch_emit as _parent_pkg

    monkeypatch.setattr(_parent_pkg, "admission", fake, raising=False)
    return fake


def _never_load(*_a, **_k):
    raise AssertionError("the entrypoint loaded before the load-conditional refusal")


def test_loaded_box_refuses_plan_route_before_loading(monkeypatch):
    fake = _install_fake_admission(
        monkeypatch, refusal={"reasons": ["cpu_load 2.0 > cpu_load_max 0.9"]}
    )
    monkeypatch.setattr(invoke_from_argv, "_load_entrypoint_main", _never_load)

    with pytest.raises(invoke_from_argv.EntrypointNotWarmLoadableError) as excinfo:
        _invoke_from_argv({
            "argv": ["--plan", "docs/plans/x.md", "--out", "x.workflow.mjs"],
            "cwd": _PROJECT_ROOT,
            "entrypoint": "emit-dispatch-workflow",
        })

    assert ipc._handler_exception_error(excinfo.value)["code"] == ipc.ENTRYPOINT_NOT_WARM_LOADABLE_ERROR
    assert fake.calls == [Path(_PROJECT_ROOT)]


@pytest.mark.parametrize("flag", ["--plan", "--inventory", "--queue"])
def test_loaded_box_refuses_every_emission_route(flag, monkeypatch):
    _install_fake_admission(monkeypatch, refusal={"reasons": ["mem_avail_pct 5 < 10"]})
    monkeypatch.setattr(invoke_from_argv, "_load_entrypoint_main", _never_load)

    with pytest.raises(invoke_from_argv.EntrypointNotWarmLoadableError):
        _invoke_from_argv({
            "argv": [flag, "whatever", "--out", "x.workflow.mjs"],
            "cwd": _PROJECT_ROOT,
            "entrypoint": "emit-dispatch-workflow",
        })


def test_idle_box_serves_warm(monkeypatch):
    fake = _install_fake_admission(monkeypatch, refusal=None)
    loaded = []
    monkeypatch.setattr(
        invoke_from_argv,
        "_load_entrypoint_main",
        lambda _s, name: (loaded.append(name), lambda *_a: 0)[1],
    )

    result = _invoke_from_argv({
        "argv": ["--plan", "docs/plans/x.md", "--out", "x.workflow.mjs"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "emit-dispatch-workflow",
    })

    assert result["exit_code"] == 0
    assert loaded == ["emit-dispatch-workflow"]
    assert fake.calls == [Path(_PROJECT_ROOT)]


def test_restamp_never_calls_should_refuse_warm(monkeypatch):
    fake = _install_fake_admission(monkeypatch, refusal={"reasons": ["loaded"]})
    loaded = []
    monkeypatch.setattr(
        invoke_from_argv,
        "_load_entrypoint_main",
        lambda _s, name: (loaded.append(name), lambda *_a: 0)[1],
    )

    result = _invoke_from_argv({
        "argv": ["--restamp", "some.workflow.mjs"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "emit-dispatch-workflow",
    })

    assert result["exit_code"] == 0
    assert fake.calls == []
    assert loaded == ["emit-dispatch-workflow"]


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_help_never_calls_should_refuse_warm(help_flag, monkeypatch):
    fake = _install_fake_admission(monkeypatch, refusal={"reasons": ["loaded"]})
    loaded = []
    monkeypatch.setattr(
        invoke_from_argv,
        "_load_entrypoint_main",
        lambda _s, name: (loaded.append(name), lambda *_a: 0)[1],
    )

    result = _invoke_from_argv({
        "argv": ["--plan", "docs/plans/x.md", help_flag],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "emit-dispatch-workflow",
    })

    assert result["exit_code"] == 0
    assert fake.calls == []


def test_unconfigured_serves_warm(monkeypatch):
    # `should_refuse_warm`'s own contract: "record when configured AND loaded;
    # None otherwise" -- unconfigured reads identically to idle at this call
    # site, which only branches on the record's presence, never its reason.
    fake = _install_fake_admission(monkeypatch, refusal=None)
    loaded = []
    monkeypatch.setattr(
        invoke_from_argv,
        "_load_entrypoint_main",
        lambda _s, name: (loaded.append(name), lambda *_a: 0)[1],
    )

    result = _invoke_from_argv({
        "argv": ["--plan", "docs/plans/x.md", "--out", "x.workflow.mjs"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "emit-dispatch-workflow",
    })

    assert result["exit_code"] == 0
    assert fake.calls == [Path(_PROJECT_ROOT)]


def test_another_allowlisted_entrypoint_with_plan_like_argv_is_unaffected(monkeypatch):
    fake = _install_fake_admission(monkeypatch, refusal={"reasons": ["loaded"]})
    loaded = []
    monkeypatch.setattr(
        invoke_from_argv,
        "_load_entrypoint_main",
        lambda _s, name: (loaded.append(name), lambda *_a: 0)[1],
    )

    result = _invoke_from_argv({
        "argv": ["--plan", "docs/plans/x.md"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })

    assert result["exit_code"] == 0
    assert fake.calls == []
    assert loaded == ["cross-repo-memo"]
