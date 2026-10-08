"""Whole-filesystem scan deny: root-shaped search roots deny, scoped searches allow."""

import pytest

from coordinator_core.bash_guards.block_whole_filesystem_scan import check

BS = chr(92)

DENY = [
    "find / -path *MetaHumanCharacterEditor*",
    "find / -iname *template_uv*",
    "find / -path /proc -prune -o -name plan-tasks.schema.json -print",
    "find -L / -name x",
    "find C:/ -name x",
    "find 'C:" + BS + "' -name x",
    "find C:" + BS + " -name x",
    "find /c -name x",
    "find /c/ -name x",
    "find /x -name x",
    "find X:" + BS + " -name x",
    "find ~ -name x",
    "find ~/ -name x",
    "cd a && find / -name x | head",
    "rg foo /",
    "rg -g '*.py' foo C:/",
    "rg -e foo /",
    "rg foo ~",
    "grep -r foo /",
    "grep -rn foo ~",
    "grep -R foo /c/",
    "grep --recursive foo /",
    "fd foo /",
    "fd foo C:/",
]

ALLOW = [
    "find C:/claude-klabauter -name x",
    "find . -name x",
    "find /x/foo -name x",
    "find . -path /x/y",
    "find . -name /",
    "find . -path / -prune",
    "grep -r foo .",
    "grep foo /",
    "grep -E '^[A-Z_0-9]*=' file.txt",
    "rg / src",
    "rg foo",
    "fd / src",
    "fd /",
    "grep -r / src",
    "echo find /",
]


def _run(cmd, tool="Bash"):
    return check({"tool_name": tool, "tool_input": {"command": cmd}})


@pytest.mark.parametrize("cmd", DENY)
def test_denies_root_scan(cmd):
    out = _run(cmd)
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("cmd", ALLOW)
def test_allows_scoped(cmd):
    assert _run(cmd) is None


def test_powershell_tool_is_matched():
    assert _run("find / -name x", tool="PowerShell")

def test_deny_is_a_floor_and_survives_a_warn_level():
    from coordinator_core import machine_profile

    assert "block-whole-filesystem-scan" in machine_profile.FLOOR_GUARDS
