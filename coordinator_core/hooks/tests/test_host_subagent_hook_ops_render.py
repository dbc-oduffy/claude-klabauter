"""The two host-subagent hook ops render a record-returning guard through the dispatcher seam."""

from __future__ import annotations

import importlib

import pytest

_OPS = [
    "coordinator_core.hooks.guard_host_subagent_bash_ban",
    "coordinator_core.hooks.guard_host_subagent_bash_spawn_shapes",
]


def _record():
    from coordinator_core.bash_guards.guard_host_subagent_bash_spawn_shapes import _deny_record

    return _deny_record("summary text", "remedy text")


@pytest.mark.parametrize("modname", _OPS)
def test_record_result_is_rendered_through_dispatch(modname, monkeypatch):
    from coordinator_core.bash_guards import dispatch

    mod = importlib.import_module(modname)
    record = _record()
    payload = {"tool_name": "Bash", "command": "echo hi"}
    monkeypatch.setattr(mod, "check", lambda params: record)

    out = mod._handler(dict(payload))
    expected = dispatch._render_record(record, payload)

    assert out == expected
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("modname", _OPS)
def test_dict_result_is_returned_unchanged(modname, monkeypatch):
    mod = importlib.import_module(modname)
    sentinel = {"hookSpecificOutput": {"permissionDecision": "deny", "x": 1}}
    monkeypatch.setattr(mod, "check", lambda params: sentinel)

    assert mod._handler({"tool_name": "Bash"}) is sentinel


@pytest.mark.parametrize("modname", _OPS)
def test_nothing_matched_yields_no_advisory(modname):
    from coordinator_core.hooks._envelope import no_advisory

    mod = importlib.import_module(modname)
    assert mod._handler({"tool_name": "Bash", "cwd": "/tmp"}) == no_advisory()


@pytest.mark.parametrize("modname", _OPS)
def test_raising_check_yields_no_advisory(modname, monkeypatch):
    from coordinator_core.hooks._envelope import no_advisory

    mod = importlib.import_module(modname)

    def boom(params):
        raise RuntimeError("x")

    monkeypatch.setattr(mod, "check", boom)
    assert mod._handler({"tool_name": "Bash"}) == no_advisory()


@pytest.mark.parametrize("modname", _OPS)
def test_raising_render_yields_no_advisory(modname, monkeypatch):
    from coordinator_core.bash_guards import dispatch
    from coordinator_core.hooks._envelope import no_advisory

    mod = importlib.import_module(modname)
    monkeypatch.setattr(mod, "check", lambda params: _record())

    def boom(record, payload):
        raise RuntimeError("x")

    monkeypatch.setattr(dispatch, "_render_record", boom)
    assert mod._handler({"tool_name": "Bash"}) == no_advisory()
