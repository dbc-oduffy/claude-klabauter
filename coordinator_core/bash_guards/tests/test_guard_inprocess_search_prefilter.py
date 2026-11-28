"""The in-process-search guard skips `search.answer` only for commands that cannot name a verb it answers."""

import sys

import pytest

from coordinator_core.bash_guards import guard_inprocess_search as gis


def test_prefilter_verbs_cover_everything_plan_for_answers():
    from coordinator_core.search.engine import GREP_FAMILY
    from coordinator_core.search.sources_read import READ_VERBS

    assert set(gis._SEARCH_VERBS) == set(GREP_FAMILY) | set(READ_VERBS) | {"ls"}


@pytest.mark.parametrize("command", ["grep -rn foo src", "g'r'ep foo f", "gr\\ep foo f", "/usr/bin/RG foo", "ls -la", "sed -n 1,5p f", "cat a | head"])
def test_a_command_naming_a_search_verb_passes_the_prefilter(command):
    assert gis._may_name_a_search_verb(command)


@pytest.mark.parametrize("command", ["echo hi", "git push origin main", "python -m pytest x.py", "git status"])
def test_a_command_naming_no_search_verb_is_skipped_without_importing_search_answer(command):
    assert not gis._may_name_a_search_verb(command)
    sys.modules.pop("coordinator_core.search.answer", None)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    assert gis.check(payload) is None
    assert "coordinator_core.search.answer" not in sys.modules


def test_a_search_command_still_reaches_the_answer():
    payload = {"tool_name": "Bash", "tool_input": {"command": "grep -rn _SEARCH_VERBS coordinator_core/bash_guards/guard_inprocess_search.py"}, "cwd": str(__import__("pathlib").Path(gis.__file__).resolve().parents[2])}
    assert gis.check(payload) is not None
