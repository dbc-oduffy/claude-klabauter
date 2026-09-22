"""An unimportable engine makes coordinator/bin/hook-run.py pass loudly, never deny.

Exit 2 on a PreToolUse hook blocks the tool, so the old `return 2` walled off
every tool call on the box whenever the engine could not be imported. These
guards are ergonomics, not security: an unrun guard now exits 0 with a
`systemMessage` for the operator and `additionalContext` for the model, and
carries no `permissionDecision` (DoE-claude coordinator/docs/wiki/
coordinator-tripwires/an-unreachable-engine-passes-loudly-never-denies.md).
"""

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

    def _unreachable():
        raise RuntimeError("no dispatch engine on this path")

    monkeypatch.setattr(cc_invoke, "require_dispatch_engine_on_path", _unreachable)
    return mod


def _run(mod, monkeypatch, event):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    code = mod.main(["hook-run", "hooks.preuse_bash_dispatch"])
    return code, json.loads(sys.stdout.getvalue())


def test_engine_down_passes_loudly_on_a_blocking_event(hook_run, monkeypatch):
    code, body = _run(hook_run, monkeypatch, {"hook_event_name": "PreToolUse"})

    assert code == 0
    assert "guard did not run" in body["systemMessage"]
    hso = body["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "did not run" in hso["additionalContext"]


def test_session_end_carries_no_hook_specific_output(hook_run, monkeypatch):
    code, body = _run(hook_run, monkeypatch, {"hook_event_name": "SessionEnd"})

    assert code == 0
    assert "hookSpecificOutput" not in body
    assert "guard did not run" in body["systemMessage"]
