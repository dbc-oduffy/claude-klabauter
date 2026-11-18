"""The ask gate refuses a sizing whose text names an executing family its top-level `family:` lacks."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401

pytestmark = pytest.mark.complete

_NAMES_AGY = "Make the change. Executing agent family: agy. Declared writes: a.py."


def _refusal(v) -> str:
    assert v.arm is None and v.halt is not None, v
    return v.halt["reason"] if v.halt["kind"] == "refusal" else ""


@pytest.mark.parametrize(
    "over",
    [
        {"em_context": _NAMES_AGY},
        {"em_analysis": {"em_brief": _NAMES_AGY}},
        {"em_analysis": {"relayed_text": _NAMES_AGY}},
        {"intent": _NAMES_AGY},
    ],
)
def test_family_named_without_top_level_family_is_refused_naming_it(repo, over):  # noqa: F811
    _put(repo, **over)
    reason = _refusal(gate(repo, REL))
    assert "agy" in reason and "family: agy" in reason and "refire" in reason


def test_a_different_top_level_family_is_refused(repo):  # noqa: F811
    _put(repo, em_context=_NAMES_AGY, family="codex")
    reason = _refusal(gate(repo, REL))
    assert "agy" in reason and "codex" in reason


def test_the_same_sizing_with_the_family_set_passes(repo):  # noqa: F811
    _put(repo, em_context=_NAMES_AGY, family="agy")
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s", v


def test_a_sizing_naming_no_family_is_unaffected(repo):  # noqa: F811
    _put(repo, em_context="Make the change in a.py.")
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s", v


def test_naming_claude_is_unaffected(repo):  # noqa: F811
    _put(repo, em_context="Executing agent family: claude.")
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s", v
