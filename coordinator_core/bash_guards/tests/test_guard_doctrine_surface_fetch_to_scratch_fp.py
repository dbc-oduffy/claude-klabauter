"""Friction #43: a fetch that names a doctrine file only in its URL, redirecting to an ungoverned path, is not a write to it."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

SURFACES = ["CLAUDE.md"]


def _denied(cmd: str) -> bool:
    return check({"tool_name": "Bash", "tool_input": {"command": cmd}}, SURFACES) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api repos/o/r/contents/CLAUDE.md > /tmp/prime.txt",
        "S=/tmp/s; gh api repos/o/r/contents/CLAUDE.md > $S/prime-july.txt",
    ],
)
def test_fetch_naming_doctrine_in_url_to_scratch_is_allowed(cmd):
    assert not _denied(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api repos/o/r/contents/CLAUDE.md > CLAUDE.md",
        "curl -o CLAUDE.md http://x/CLAUDE.md > /tmp/x",
        "S=CLAUDE.md; gh api x/CLAUDE.md > $S",
        "gh api x/CLAUDE.md | tee CLAUDE.md > /tmp/x",
    ],
)
def test_fetch_that_writes_the_doctrine_file_still_denies(cmd):
    assert _denied(cmd)
