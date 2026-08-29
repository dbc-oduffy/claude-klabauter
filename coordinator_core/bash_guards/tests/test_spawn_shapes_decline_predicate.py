"""coordinator_core.bash_guards.tests.test_spawn_shapes_decline_predicate --
pins `guard_host_subagent_bash_spawn_shapes._declines_for_inprocess_answer`
to the fix for state/bug-backlog/2026-08-29-the-guard-rehome-is-not-yet-
safe-to-dele-9f7396118b81.yaml finding 1: the decline must be conditional on
`coordinator_core.search.answer.plan_for` actually answering the WHOLE
command, not on the precedence-winning shape alone.

Negative-spec:
  - Does NOT re-test `plan_for` itself (coordinator_core/search/tests/ owns
    that) -- these cases assert the PREDICATE's use of it: decline only when
    `plan_for` returns a plan, stay in scope (deny-eligible) otherwise.
  - Does NOT exercise `guard_inprocess_search.check()` -- only its
    `_DISABLE_ENV_VAR` constant, imported (not duplicated) by the predicate.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import guard_host_subagent_bash_spawn_shapes as _mod
from coordinator_core.bash_guards._dialect import Dialect
from coordinator_core.bash_guards._shape_classifier import Shape, classify_command
from coordinator_core.bash_guards.guard_inprocess_search import _DISABLE_ENV_VAR


def _primary_shape(cmd: str, dialect: Dialect):
    classification = classify_command(cmd, dialect=dialect)
    assert classification.primary is not None, f"{cmd!r} classified no primary shape"
    return classification.primary.shape


@pytest.fixture(autouse=True)
def _clean_disable_env():
    prior = os.environ.pop(_DISABLE_ENV_VAR, None)
    yield
    if prior is None:
        os.environ.pop(_DISABLE_ENV_VAR, None)
    else:
        os.environ[_DISABLE_ENV_VAR] = prior


# Measured (backlog finding 1) fully `plan_for`-answerable GREP_VIA_BASH commands.
_ANSWERABLE_BASH = [
    'grep -rn "foo" .',
    'grep -rn "foo" . | sort | uniq -c',
    "grep -rln TODO src tests docs",
]

# Measured NOT fully answerable despite the same precedence-winning shape --
# the general defect this predicate exists to close.
_UNANSWERABLE_BASH = [
    "curl -s foo | grep bar",  # piped INTO -- input doesn't exist until curl runs
    "grep -rn foo . ; echo done",  # trailing `;`-joined segment, not pipe-connected
]


@pytest.mark.parametrize("cmd", _ANSWERABLE_BASH)
def test_declines_when_seam_plans_the_whole_command(cmd):
    shape = _primary_shape(cmd, Dialect.BASH)
    assert shape is Shape.GREP_VIA_BASH
    assert _mod._declines_for_inprocess_answer(shape, cmd, "Bash") is True


@pytest.mark.parametrize("cmd", _UNANSWERABLE_BASH)
def test_denies_when_seam_cannot_plan_the_whole_command(cmd):
    shape = _primary_shape(cmd, Dialect.BASH)
    assert shape is Shape.GREP_VIA_BASH
    assert _mod._declines_for_inprocess_answer(shape, cmd, "Bash") is False


def test_denies_when_primary_shape_is_not_grep_via_bash():
    cmd = 'for f in *.py; do grep foo "$f"; done'
    shape = _primary_shape(cmd, Dialect.BASH)
    assert shape is not Shape.GREP_VIA_BASH
    assert _mod._declines_for_inprocess_answer(shape, cmd, "Bash") is False


def test_denies_when_inprocess_search_is_disabled():
    cmd = 'grep -rn "foo" .'
    shape = _primary_shape(cmd, Dialect.BASH)
    os.environ[_DISABLE_ENV_VAR] = "1"
    assert _mod._declines_for_inprocess_answer(shape, cmd, "Bash") is False


def test_denies_when_the_seam_import_fails(monkeypatch):
    cmd = 'grep -rn "foo" .'
    shape = _primary_shape(cmd, Dialect.BASH)

    real_import = __import__

    def _blow_up_on_search_answer(name, *args, **kwargs):
        if name == "coordinator_core.search.answer":
            raise ImportError("simulated seam import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blow_up_on_search_answer)
    assert _mod._declines_for_inprocess_answer(shape, cmd, "Bash") is False


def test_denies_when_the_disable_flag_lookup_import_fails(monkeypatch):
    cmd = 'grep -rn "foo" .'
    shape = _primary_shape(cmd, Dialect.BASH)

    real_import = __import__

    def _blow_up_on_inprocess_search(name, *args, **kwargs):
        if name == "coordinator_core.bash_guards.guard_inprocess_search":
            raise ImportError("simulated seam import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blow_up_on_inprocess_search)
    assert _mod._declines_for_inprocess_answer(shape, cmd, "Bash") is False


def test_powershell_command_stays_in_scope_and_denies():
    """`Select-String` does not classify as `Shape.GREP_VIA_BASH` under the
    PowerShell dialect (that's a bash-basename-keyed shape, so this command
    classifies with no primary match at all), so this stays fully in scope
    regardless of the seam -- included so `tool_name` plumbing through the
    predicate is exercised for the PowerShell dialect too, not just Bash."""
    cmd = "Select-String -Pattern foo -Path . -Recurse"
    classification = classify_command(cmd, dialect=Dialect.POWERSHELL)
    shape = classification.primary.shape if classification.primary else None
    assert shape is not Shape.GREP_VIA_BASH
    assert _mod._declines_for_inprocess_answer(shape, cmd, "PowerShell") is False


def test_declines_uses_the_actual_tool_name_for_plan_for(monkeypatch):
    """The predicate must pass `tool_name` through to `plan_for` rather than
    hardcoding "Bash" -- pinned by asserting the call the predicate makes."""
    seen = {}

    def _fake_plan_for(cmd, tool_name="Bash"):
        seen["cmd"] = cmd
        seen["tool_name"] = tool_name
        return object()

    monkeypatch.setattr(
        "coordinator_core.search.answer.plan_for", _fake_plan_for
    )
    cmd = 'grep -rn "foo" .'
    result = _mod._declines_for_inprocess_answer(Shape.GREP_VIA_BASH, cmd, "Bash")
    assert result is True
    assert seen == {"cmd": cmd, "tool_name": "Bash"}
