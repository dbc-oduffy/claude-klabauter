"""A guard returning a bare dict crashes in the dispatcher; the crash resolves by the one
``deny_registry`` rule: an irreversible-class guard keeps its deny, any other becomes an
advisory and the command is allowed. Drives the real ``evaluate_payload_json`` loop over a
controlled chain."""

from __future__ import annotations

import json

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

REGISTERED = "block-worktree-tool"
UNREGISTERED = "fake-unregistered-bare-dict-guard"


def _bare_dict() -> dict:
    return {"decision": "block", "reason": "legacy bare dict"}


def _run(monkeypatch, name: str):
    chain = [GuardEntry(name, _bare_dict, True, GuardBand.CONFINEMENT_DENY)]
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: list(chain))
    raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo probe"}, "cwd": "."})
    return dispatch.evaluate_payload_json(raw)


def test_registered_irreversible_guard_keeps_its_deny(monkeypatch):
    assert deny_registry.guard_deny_class(REGISTERED) in deny_registry.IRREVERSIBLE_CLASSES
    assert REGISTERED not in dispatch._PINNED_DICT_RETURNS
    assert REGISTERED not in dispatch._CRASH_TRIGGER_SUBSTRINGS
    out = _run(monkeypatch, REGISTERED)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_unregistered_guard_becomes_advisory_and_allows(monkeypatch):
    assert deny_registry.guard_deny_class(UNREGISTERED) is None
    out = _run(monkeypatch, UNREGISTERED)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"]
