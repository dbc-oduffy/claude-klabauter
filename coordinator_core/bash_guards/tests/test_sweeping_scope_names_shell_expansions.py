"""A commit scope held in a shell variable is refused for what it is.

`git commit -- "${P[@]}"` was refused as "names a directory": false about the
command, so the operator reads a parser bug rather than the real objection --
an expansion the guard cannot read back.
"""

from coordinator_core.bash_guards.dispatch_checks import _sweeping_scope_sentence


def test_a_shell_expansion_is_named_as_one():
    sentence = _sweeping_scope_sentence(['"${P[@]}"'])
    assert "shell expansion" in sentence
    assert "directory" not in sentence


def test_a_real_directory_is_still_named_as_one():
    assert "names a directory" in _sweeping_scope_sentence(["state/"])
