"""A git mutation's exit status read through a pipe.

`git commit -F msg.txt | tail -3 && git push` tests TAIL. A pipeline's status is
its LAST stage's, so a failed commit reads as success to the harness and to any
`&&` after it, and the push runs against a commit that never landed.

Measured 2026-09-11 in two repos independently, by two sessions who were at that
moment each telling the other to distrust unverified green signals. One of them
had misreported a publish round's exit 1 as exit 0 by the same mechanism. The
habit behind it is benign -- piping to `tail`/`head` to keep output short --
which is why it survives: invisible until the first stage fails, and invisible
again then.

Both verdicts are proven: a read-only pipeline and an unpiped chain stay quiet.
"""

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
    """The refusal quotes what it parsed. A message naming the whole line leaves
    the reader to find which stage was the problem, which on a three-stage chain
    is the question they came with."""
    head = dispatch_checks._piped_exit_code_chain(
        "git add -- R.md && git commit -F m.txt | tail -3 && git push origin main"
    )
    assert head == "git commit -F m.txt"


def test_flags_taking_a_value_do_not_hide_the_subcommand():
    """`-C <path>` and `-c <k=v>` put a value between the flag and the
    subcommand. A hand-rolled pattern that stops at the first non-flag token
    reads the PATH as the subcommand and the detector goes quiet exactly where a
    scripted call sits."""
    assert dispatch_checks._piped_exit_code_chain(
        "git -C X:/claude-klabauter commit -F m.txt | tail -1"
    ) is not None
