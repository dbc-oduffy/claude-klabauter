
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook_run(monkeypatch):
    spec = importlib.util.spec_from_file_location("hook_run_under_test", _BIN / "hook-run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if str(_BIN / "lib") not in sys.path:
        monkeypatch.syspath_prepend(str(_BIN / "lib"))
    import cc_invoke
    import coordinator_core.ipc as ipc

    seen = {}

    def _capture(op, params, origin_worktree=None):
        seen["params"] = params
        return {}

    monkeypatch.setattr(cc_invoke, "require_dispatch_engine_on_path", lambda: None)
    monkeypatch.setattr(ipc, "dispatch_from_hook", _capture)
    return mod, seen


def _run(mod, monkeypatch, event):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert mod.main(["hook-run", "hooks.preuse_bash_dispatch"]) == 0


_EVENT = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}


def test_the_callers_overrides_reach_the_payload_and_nothing_else(hook_run, monkeypatch):
    mod, seen = hook_run
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BLANKET_ADD", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "never")

    _run(mod, monkeypatch, dict(_EVENT))

    env = seen["params"]["payload"]["env"]
    assert env["COORDINATOR_OVERRIDE_BLANKET_ADD"] == "1"
    assert "UNRELATED_SECRET" not in env


def test_an_event_that_carries_env_is_not_overwritten(hook_run, monkeypatch):
    mod, seen = hook_run
    monkeypatch.setenv("COORDINATOR_OVERRIDE_BLANKET_ADD", "1")

    _run(mod, monkeypatch, {**_EVENT, "env": {"COORDINATOR_ALLOW_X": "1"}})

    assert seen["params"]["payload"]["env"] == {"COORDINATOR_ALLOW_X": "1"}
