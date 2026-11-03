"""with-tier-t-slot launches a pytest child inside spawn_in_capped_job with the depth sentinel; other commands stay a plain Popen."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_WRAPPER = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "with-tier-t-slot"


@pytest.fixture()
def wrapper():
    loader = importlib.machinery.SourceFileLoader("with_tier_t_slot_capped_under_test", str(_WRAPPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Proc:
    def wait(self):
        return 7


def _arrange(wrapper, monkeypatch):
    from coordinator_core.diagnostics import contained_run

    calls = {"capped": [], "closed": 0, "popen": []}

    def fake_capped(argv, *, popen_kwargs=None, **kw):
        calls["capped"].append((list(argv), dict(popen_kwargs or {})))

        def close():
            calls["closed"] += 1

        return _Proc(), close

    monkeypatch.setattr(contained_run, "spawn_in_capped_job", fake_capped)
    monkeypatch.setattr(wrapper.subprocess, "Popen", lambda argv, **kw: calls["popen"].append(argv) or _Proc())
    monkeypatch.setattr(wrapper, "_win32_resolve_command", lambda c: list(c))
    monkeypatch.setattr(wrapper.tier_t_slots, "acquire_slot", lambda *a, **k: object())
    monkeypatch.setattr(wrapper.tier_t_slots, "release_slot", lambda s: None)
    return calls


@pytest.mark.parametrize(
    "command",
    [["python", "-m", "pytest", "tests/test_x.py"], ["pytest", "tests/test_x.py"]],
)
def test_pytest_child_runs_in_capped_job_with_depth_sentinel(wrapper, monkeypatch, command):
    from coordinator_core.testing import spawn_depth

    calls = _arrange(wrapper, monkeypatch)
    rc = wrapper.main(["--", *command])
    assert rc == 7
    assert calls["popen"] == []
    (argv, kwargs), = calls["capped"]
    assert argv == command
    assert kwargs["env"][spawn_depth.ENV_KEY] == str(spawn_depth.child_depth(os.environ))
    assert calls["closed"] == 1


def test_non_pytest_command_stays_a_plain_popen(wrapper, monkeypatch):
    calls = _arrange(wrapper, monkeypatch)
    assert wrapper.main(["--", "npx", "vitest", "run"]) == 7
    assert calls["capped"] == []
    assert calls["popen"] == [["npx", "vitest", "run"]]


def test_close_runs_when_the_wait_raises(wrapper, monkeypatch):
    calls = _arrange(wrapper, monkeypatch)

    class _Boom(_Proc):
        def wait(self):
            raise KeyboardInterrupt

    from coordinator_core.diagnostics import contained_run

    def capped(argv, *, popen_kwargs=None, **kw):
        return _Boom(), lambda: calls.__setitem__("closed", calls["closed"] + 1)

    monkeypatch.setattr(contained_run, "spawn_in_capped_job", capped)
    monkeypatch.setattr(wrapper, "_stop_child", lambda p: None)
    assert wrapper.main(["--", "python", "-m", "pytest", "t.py"]) == 130
    assert calls["closed"] == 1


def test_is_pytest_command_refuses_lookalikes(wrapper):
    assert not wrapper._is_pytest_command(["python", "script.py", "-m", "pytest"])
    assert not wrapper._is_pytest_command(["npx", "vitest"])
    assert not wrapper._is_pytest_command([])
