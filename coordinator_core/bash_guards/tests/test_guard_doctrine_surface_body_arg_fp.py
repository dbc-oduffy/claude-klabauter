"""A doctrine file named only inside a quoted free-text flag value (``--body '...'``)
is content, not a write target; a redirect, tee or cp/mv/install into it still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

SURFACES = ["CLAUDE.md", "coordinator.local.md"]
QA = "coordinator-queue-append --schema bug-backlog --title t"


def _denied(cmd: str, tool: str = "Bash") -> bool:
    return check({"tool_name": tool, "tool_input": {"command": cmd}}, SURFACES) is not None


ALLOWED = [
    f"{QA} --body 'the guard blocked edits to coordinator.local.md in prose'",
    f'{QA} --body "see CLAUDE.md for the rule"',
    f"{QA} --body='CLAUDE.md is mentioned here'",
    f"{QA} --body 'edit CLAUDE.md > later; then cp a CLAUDE.md'",
    f"{QA} --body 'names CLAUDE.md' > scratch/out.txt",
    f"{QA} --body 'names CLAUDE.md' | tee scratch/out.txt",
    f"{QA} --body 'names CLAUDE.md' && cp a.txt scratch/b.txt",
    f"{QA} --body 'guard denies patch of coordinator.local.md'",
    f'{QA} --body "we ed coordinator.local.md with python and tee"',
    f"{QA} --body 'sed -i on CLAUDE.md, rsync of CLAUDE.md, ex CLAUDE.md'",
]

DENIED = [
    f"{QA} --body 'names CLAUDE.md' > CLAUDE.md",
    f"{QA} --body 'names CLAUDE.md' >> coordinator.local.md",
    f"{QA} --body 'names CLAUDE.md' | tee CLAUDE.md",
    f"{QA} --body 'names CLAUDE.md' && cp scratch/a.md CLAUDE.md",
    f"{QA} --body 'names CLAUDE.md' && mv scratch/a.md CLAUDE.md",
    f"{QA} --body 'names CLAUDE.md' && install scratch/a.md CLAUDE.md",
    f"{QA} --body 'names CLAUDE.md' --exec 'tee CLAUDE.md'",
    f'{QA} --body "names CLAUDE.md `tee CLAUDE.md`"',
    f"{QA} --body 'x' && python3 -c \"open('CLAUDE.md','w')\"",
    f"{QA} --body 'x' && sed -i s/a/b/ CLAUDE.md",
    f"{QA} --body 'x'\ncat > CLAUDE.md <<'EOF'\nbody\nEOF",
]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", ALLOWED)
def test_body_arg_naming_doctrine_file_is_allowed(cmd, tool):
    assert not _denied(cmd, tool)


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", DENIED)
def test_real_doctrine_write_still_denies(cmd, tool):
    assert _denied(cmd, tool)
