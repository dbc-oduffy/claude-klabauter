
from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch_checks


FIRES = [
    "git add -- README.md && git commit -F msg.txt | tail -3 && git push origin main",
    "git commit -F msg.txt -- a.py | tail -3",
    "git -C X:/repo commit -F msg.txt | tail -1 && git push",
    "git -c user.name=x commit -F msg.txt | tail -1 && echo ok",
    "git push origin main | tail -1 && echo done",
]

QUIET = [
    "git commit -F msg.txt -- a.py && git push",
    "git commit -F msg.txt -- a.py",
    "git log --oneline | head -5 && git status",
    "git status --porcelain | head -20",
    "git diff --cached --name-only | wc -l",
]


@pytest.mark.parametrize("cmd", FIRES)
def test_a_piped_git_mutation_is_named(cmd):
    head = dispatch_checks._piped_exit_code_chain(cmd)
    assert head is not None, cmd
    assert head.startswith("git"), head


@pytest.mark.parametrize("cmd", QUIET)
def test_a_read_only_or_unpiped_command_is_quiet(cmd):
    assert dispatch_checks._piped_exit_code_chain(cmd) is None, cmd


def test_the_named_segment_is_the_git_command_not_the_whole_chain():
    head = dispatch_checks._piped_exit_code_chain(
        "git add -- R.md && git commit -F m.txt | tail -3 && git push origin main"
    )
    assert head == "git commit -F m.txt"


def test_flags_taking_a_value_do_not_hide_the_subcommand():
    assert dispatch_checks._piped_exit_code_chain(
        "git -C X:/claude-klabauter commit -F m.txt | tail -1"
    ) is not None
