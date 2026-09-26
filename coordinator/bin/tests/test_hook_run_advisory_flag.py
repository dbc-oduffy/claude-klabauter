"""`hook-run --advisory <op>` silences every failure class, guards stay loud.

An advisory hook (e.g. the example-retrieval-repo search-steering nudge) must never
harass an agent for using Grep/Glob just because its op is not yet published
on this clone's engine -- METHOD_NOT_FOUND alone cannot tell the caller that,
because the op isn't there to say so (state/cross-repo/inbox/
2026-09-25-doe-claude-em-advisory-hooks-fail-silent.md). The caller declares
it instead, with `--advisory` immediately before the op name. Guards (no
flag) keep the existing loud did-not-run envelope -- pinned separately by
`test_hook_run_engine_down_passes_loudly.py`, not weakened here.
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

    monkeypatch.setattr(cc_invoke, "require_dispatch_engine_on_path", lambda: None)
    return mod


def _run(mod, monkeypatch, event, argv):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = mod.main(argv)
    return code, out.getvalue()


_EVENT = {"hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "x"}}


def test_advisory_method_not_found_is_silent(hook_run, monkeypatch):
    """The exact failure the memo names: an unpublished op raises
    HookDispatchError(METHOD_NOT_FOUND); under --advisory this is empty
    stdout, exit 0 -- never the loud did-not-run envelope."""
    import coordinator_core.ipc as ipc

    def _raise(op, params, origin_worktree=None):
        raise ipc.HookDispatchError(op, ipc.METHOD_NOT_FOUND, "Method not found")

    monkeypatch.setattr(ipc, "dispatch_from_hook", _raise)

    mod = hook_run
    code, stdout = _run(
        mod, monkeypatch, dict(_EVENT),
        ["hook-run", "--advisory", "hooks.preuse_search_dispatch"],
    )

    assert code == 0
    assert stdout == ""


def test_advisory_engine_unreachable_is_silent(hook_run, monkeypatch):
    import cc_invoke

    def _unreachable():
        raise RuntimeError("no dispatch engine on this path")

    monkeypatch.setattr(cc_invoke, "require_dispatch_engine_on_path", _unreachable)

    mod = hook_run
    code, stdout = _run(
        mod, monkeypatch, dict(_EVENT),
        ["hook-run", "--advisory", "hooks.preuse_search_dispatch"],
    )

    assert code == 0
    assert stdout == ""


def test_advisory_success_passes_through_unchanged(hook_run, monkeypatch):
    import coordinator_core.ipc as ipc

    def _ok(op, params, origin_worktree=None):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "steer"}}

    monkeypatch.setattr(ipc, "dispatch_from_hook", _ok)

    mod = hook_run
    code, stdout = _run(
        mod, monkeypatch, dict(_EVENT),
        ["hook-run", "--advisory", "hooks.preuse_search_dispatch"],
    )

    assert code == 0
    body = json.loads(stdout)
    assert body["hookSpecificOutput"]["additionalContext"] == "steer"


def test_guard_without_the_flag_stays_loud_on_method_not_found(hook_run, monkeypatch):
    import coordinator_core.ipc as ipc

    def _raise(op, params, origin_worktree=None):
        raise ipc.HookDispatchError(op, ipc.METHOD_NOT_FOUND, "Method not found")

    monkeypatch.setattr(ipc, "dispatch_from_hook", _raise)

    mod = hook_run
    code, stdout = _run(
        mod, monkeypatch, dict(_EVENT),
        ["hook-run", "hooks.preuse_bash_dispatch"],
    )

    assert code == 0
    body = json.loads(stdout)
    assert "guard did not run" in body["systemMessage"]
    assert "did not run" in body["hookSpecificOutput"]["additionalContext"]
