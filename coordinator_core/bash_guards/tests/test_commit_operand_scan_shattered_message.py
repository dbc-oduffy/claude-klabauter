"""A quoted commit message that shatters under `shlex` must not be read as a
pathspec.

The hazard: the canonical multi-line commit form is
`git commit -m "$(cat <<'EOF' ... EOF\n)"`. When the heredoc BODY contains a
double quote -- quoting the PM, naming a config value, citing a guard message --
that inner quote closes the outer one, and `shlex.split` shatters the message
into several bare tokens. The tail fragment carries the heredoc terminator and
its newlines.

Both polarities are real and were both live before this test:

  - FALSE NEGATIVE (the serious one): a genuinely BARE `git commit -m "..."`
    with no pathspec at all scans as carrying positional operands, so
    `_bt_commit_has_explicit_pathspec` reports explicit scope and SUPPRESSES
    the bare-commit deny -- on exactly the shape it exists to catch. Same
    defect class as the 2026-08-05 surviving-redirection fix, same polarity:
    shell syntax counted as an operand silences the check.

  - FALSE POSITIVE: a correctly scoped `... -- <paths>` denied, because the
    shattered residue sits BEFORE the separator. Reported by claude-klabauter-24,
    who hit it twice on a commit carrying a correct pathspec and got through
    only by removing the inner quotes -- which teaches the reader that the
    pathspec form does not work.

The rule the fix encodes: a fragment after a standalone `--` is dropped (git's
grammar already proves everything there is a pathspec), while a fragment with
NO separator anywhere renders the parse ambiguous, which fails OPEN per
SC-DR-020 bound 4.
"""

import shlex

import pytest

from coordinator_core.bash_guards import dispatch_checks as d


def _scope(cmd: str) -> bool:
    return d._bt_commit_has_explicit_pathspec(shlex.split(cmd, posix=True))


#: A heredoc message body carrying an inner double-quoted phrase -- the shape
#: that shatters. Kept as one constant so both polarities below are provably
#: the same message, differing only in whether a pathspec follows.
_SHATTERING_MSG = (
    "\"$(cat <<'EOF'\n"
    "subject line\n"
    "\n"
    'The PM said "kill them all" and that is the ruling.\n'
    "EOF\n"
    ')"'
)


def test_shattered_message_alone_is_not_scope():
    """The false negative. No pathspec is present, so the bare-commit deny
    must stay armed."""
    assert _scope("git commit -q -m " + _SHATTERING_MSG) is False


def test_shattered_message_does_not_defeat_a_real_pathspec():
    """The false positive. The same message WITH a correct `-- <paths>` is
    scoped, and the pre-separator residue must not deny it."""
    assert _scope("git commit -q -m " + _SHATTERING_MSG + " -- foo.py") is True


def test_shattered_message_does_not_defeat_a_multi_path_pathspec():
    assert _scope(
        "git commit -q -m " + _SHATTERING_MSG + " -- foo.py bar/baz.py"
    ) is True


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # A clean multi-line message is still unscoped when bare...
        ("git commit -m \"$(cat <<'EOF'\nsubject\n\nplain body.\nEOF\n)\"", False),
        # ...and still scoped when a pathspec follows.
        (
            "git commit -m \"$(cat <<'EOF'\nsubject\n\nplain body.\nEOF\n)\" -- a.py",
            True,
        ),
        ("git commit -m subject", False),
        ("git commit -m x a.py", True),
        ("git commit -m x -- a.py", True),
        ("git commit -m x --pathspec-from-file=list.txt", True),
        # `--include` merges into the staged index and commits the union, so
        # it is never scope -- the negative spec this walk already carried.
        ("git commit -i -m x a.py", False),
    ],
)
def test_unshattered_shapes_are_unchanged(cmd: str, expected: bool):
    """Pins the pre-existing verdicts the fix must not move."""
    assert _scope(cmd) is expected


def test_newline_bearing_operand_is_the_discriminator():
    """The predicate itself, stated directly: a command-line pathspec never
    carries a newline, because git receives argv from the shell."""
    assert d._bt_is_shattered_operand("all here.\nEOF\n)") is True
    assert d._bt_is_shattered_operand("foo.py") is False
    assert d._bt_is_shattered_operand("path with spaces.py") is False
