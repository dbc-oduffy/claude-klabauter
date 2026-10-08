"""Pattern and option arguments are never filenames to the Bash-arm illegal-filename advisory."""

import pytest

from coordinator_core.bash_guards import block_illegal_filename as m


def _check(cmd):
    return m.check({"tool_name": "Bash", "tool_input": {"command": cmd}})


@pytest.mark.parametrize("cmd", [
    "grep -E '^[A-Z_0-9]*=' file.txt",
    "grep -o -E '^[A-Z_0-9]*=' file.txt",
    "cat f | grep -o '^[A-Z_0-9]*=' > out.txt",
    "rg -o 'a*b' src",
    "sed -n -o 'a*b' f",
    "find . -name 'a*' -o -name 'b?'",
    "ssh -o 'StrictHostKeyChecking=no' host",
    "echo mv a 'b*c'",
    "grep -w mv 'x*y' f",
])
def test_pattern_argument_is_not_rewritten(cmd):
    assert _check(cmd) is None


def test_real_file_targets_still_checked():
    assert _check("mv a.txt 'b*c.txt'") is not None
    assert _check("echo x > 'b*c.txt'") is not None
    assert _check("cd d && mv a.txt 'b*c.txt'") is not None
