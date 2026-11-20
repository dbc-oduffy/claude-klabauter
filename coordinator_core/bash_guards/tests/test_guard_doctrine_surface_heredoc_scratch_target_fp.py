"""A heredoc whose redirect target is a scratch file is judged by that target, not by
doctrine filenames in its body; a heredoc targeting a doctrine file still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

SURFACES = ["CLAUDE.md", "coordinator.local.md"]


def _denied(cmd: str, tool: str = "Bash") -> bool:
    return check({"tool_name": tool, "tool_input": {"command": cmd}}, SURFACES) is not None


ALLOWED = [
    "cat > scratch/note.md <<'EOF'\nedit CLAUDE.md and coordinator.local.md\nEOF",
    "cat > scratch/note.md <<EOF\nsee CLAUDE.md\nEOF",
    "cat >> scratch/note.md <<'EOF'\nsed -i s/a/b/ CLAUDE.md\nEOF",
    "cat <<'EOF' > scratch/note.md\ncp x CLAUDE.md\ntee CLAUDE.md > CLAUDE.md\nEOF",
    "cat > /tmp/x/handoff.md <<'EOF'\npython3 -c \"open('CLAUDE.md','w')\"\nEOF",
    "mkdir -p scratch && cat > scratch/note.md <<'EOF'\nCLAUDE.md > CLAUDE.md\nEOF",
]

DENIED = [
    "cat > CLAUDE.md <<'EOF'\nbody\nEOF",
    "cat >> coordinator.local.md <<'EOF'\nbody\nEOF",
    "cat <<'EOF' > CLAUDE.md\nbody\nEOF",
    "cat > scratch/a.md <<'EOF'\nbody\nEOF\ncat > CLAUDE.md <<'EOF'\nbody\nEOF",
    "cat > scratch/a.md <<'EOF'\nCLAUDE.md\nEOF\ntee CLAUDE.md < scratch/a.md",
    "tee CLAUDE.md <<'EOF'\nbody\nEOF",
    "cat > scratch/a.md <<'EOF'\nCLAUDE.md\nEOF\nsed -i s/a/b/ CLAUDE.md",
    "python3 <<'PY'\nopen('CLAUDE.md','w').write('x')\nPY",
]


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", ALLOWED)
def test_scratch_heredoc_mentioning_doctrine_file_is_allowed(cmd, tool):
    assert not _denied(cmd, tool)


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("cmd", DENIED)
def test_heredoc_writing_doctrine_file_still_denies(cmd, tool):
    assert _denied(cmd, tool)
