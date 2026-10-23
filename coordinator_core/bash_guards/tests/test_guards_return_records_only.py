"""Completion test for the typed-record migration: no guard hands the dispatcher a bare envelope.

Two claims. (1) `dispatch._admit_guard_result` accepts an AlternativeRecord or None and
crash-routes any other return, except a non-deny dict from a name pinned in
`dispatch._PINNED_DICT_RETURNS`. (2) Across non-test `bash_guards` modules an AST scan finds a
"hookSpecificOutput" dict literal only in `dispatch.py` plus a count-pinned exception set.
The corpus-wide "every speaking row returns a record" claim is
`test_no_guard_builds_bare_envelope.py`'s. The pin tables are shrink-only: an entry that no
longer holds fails the test and is deleted, never raised.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.guard_messages.alternative_record import AlternativeRecord, Override, ReadyCommand
from coordinator_core.guard_messages.render import override_pointer

_PKG = Path(dispatch.__file__).resolve().parent

#: module -> (count of "hookSpecificOutput" dict literals, reason). dispatch.py is the author of
#: envelopes and is not listed.
_LITERAL_EXCEPTIONS: Dict[str, tuple] = {
    "_advisory_value.py": (1, "re-shapes an already rendered envelope (strips additionalContext)"),
}

_PINNED_DICT_COUNT = 7


def _literal_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for py in sorted(_PKG.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        n = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant) and key.value == "hookSpecificOutput"
        )
        if n and py.name != "dispatch.py":
            counts[py.name] = n
    return counts


def test_hook_specific_output_literal_lives_in_dispatch_or_a_pinned_exception():
    found = _literal_counts()
    pinned = {name: count for name, (count, _reason) in _LITERAL_EXCEPTIONS.items()}
    assert found == pinned, "dict-literal envelope builders drifted from the pin: %s vs %s" % (found, pinned)
    assert all(reason for _count, reason in _LITERAL_EXCEPTIONS.values())


def test_pinned_dict_returns_are_count_pinned_with_a_reason_each():
    assert len(dispatch._PINNED_DICT_RETURNS) == _PINNED_DICT_COUNT
    assert all(dispatch._PINNED_DICT_RETURNS.values())


_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}
_ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
_PINNED = next(iter(dispatch._PINNED_DICT_RETURNS))


def test_admit_passes_none_and_renders_a_record():
    assert dispatch._admit_guard_result("g", None, {}) is None
    rec = AlternativeRecord("advisory", "note.", (ReadyCommand(text="ls"),), "g", em_text="plain")
    assert dispatch._admit_guard_result("g", rec, {})["hookSpecificOutput"]["additionalContext"].endswith("plain")


def test_admit_refuses_a_bare_dict_from_an_unpinned_guard():
    with pytest.raises(TypeError):
        dispatch._admit_guard_result("not-pinned", _ALLOW, {})


def test_admit_refuses_a_deny_dict_even_from_a_pinned_guard():
    with pytest.raises(TypeError):
        dispatch._admit_guard_result(_PINNED, _DENY, {})


def test_admit_passes_a_non_deny_dict_from_a_pinned_guard():
    assert dispatch._admit_guard_result(_PINNED, _ALLOW, {}) is _ALLOW


@pytest.mark.parametrize("bad", [[_ALLOW], "text", 3])
def test_admit_refuses_a_non_record_non_dict(bad):
    with pytest.raises(TypeError):
        dispatch._admit_guard_result(_PINNED, bad, {})


def test_a_dict_returning_fail_closed_guard_is_crash_denied(monkeypatch):
    entry = dispatch.GuardEntry(
        "fake-legacy-guard", lambda: _ALLOW, True, dispatch.GuardBand.CONFINEMENT_DENY
    )
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": "s3n"}
    import json

    out = dispatch.evaluate_payload_json(json.dumps(payload))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "fake-legacy-guard guard crashed" in hso["permissionDecisionReason"]


def test_em_text_record_skips_the_override_note(monkeypatch):
    calls: List[str] = []
    monkeypatch.setattr(
        "coordinator_core.bash_guards._helpers.operator_override_note",
        lambda *a, **k: calls.append("called") or "NOTE",
    )
    from coordinator_core.guard_messages.alternative_record import EscalationRoute

    members = (EscalationRoute("it", "the bypass"), Override("K"))
    with_text = AlternativeRecord("deny", "s.", members, "g", em_text="verbatim")
    assert "NOTE" not in str(dispatch._render_record(with_text, {}))
    assert calls == []


def test_operator_override_note_delegates_to_the_moved_wording(monkeypatch):
    from coordinator_core.bash_guards import _helpers

    monkeypatch.setattr(_helpers, "resolves_em_audience", lambda payload, git_root: True)
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: True)
    assert operator_override_note("COORDINATOR_X", payload={}) == override_pointer(
        _helpers.OVERRIDE_KEYS_DOC_DISPLAY
    )
    monkeypatch.setattr(_helpers, "resolves_em_audience", lambda payload, git_root: False)
    assert operator_override_note("COORDINATOR_X", payload={}) == ""
    monkeypatch.setattr(_helpers, "resolves_em_audience", lambda payload, git_root: True)
    monkeypatch.setattr(_helpers, "_override_keys_doc_reachable", lambda: False)
    assert operator_override_note("COORDINATOR_X", payload={}) == ""
