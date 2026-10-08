"""Small literal for-loops and plain echo separators do not trigger the spawn advisory."""

import pytest

from coordinator_core.bash_guards.guard_plumbing_and_loops import check


def _check(cmd):
    return check({"tool_name": "Bash", "tool_input": {"command": cmd}})


@pytest.mark.parametrize("cmd", [
    "for f in a b; do git log -1 $f; done",
    "for f in a b c; do git log -1 $f; done",
    "git status; echo ----; git log -1",
    "git status; echo ====; git log -1",
])
def test_one_shot_diagnostics_are_silent(cmd):
    assert _check(cmd) is None


@pytest.mark.parametrize("cmd", [
    "for f in a b c d; do git log -1 $f; done",
    "for f in *.txt; do rm $f; done",
    "for f in $(ls); do wc -l $f; done",
    "for f in a b; do find . -name $f -exec rm {} + ; done",
    "git status; echo done; git log -1",
    "git status; echo $?",
])
def test_genuine_spawn_shapes_still_advise(cmd):
    assert _check(cmd) is not None
