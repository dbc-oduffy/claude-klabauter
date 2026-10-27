"""A state/sizings path named inside a quoted argument or a python heredoc
string literal is a mention, not a write, even next to a heredoc; a real
redirect, tee, cp or mv into the store still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

S = "state/sizings/x.yaml"
SURFACES = ["CLAUDE.md"]


def _denied(cmd: str) -> bool:
    out = check({"tool_name": "Bash", "tool_input": {"command": cmd}}, SURFACES)
    return out is not None


IMP = (
    "from coordinator_core.bash_guards.guard_doctrine_surface_bash_write "
    "import _sizing_write_denied as d"
)
OP = "coordinator-invoke plan.readiness_verdict"

ALLOWED = [
    f"{OP} '{{\"sizing\":\"{S}\"}}'\ncat > scratch/out.txt <<'EOF'\nbody\nEOF",
    f"cat > scratch/out.txt <<'EOF'\nbody\nEOF\n{OP} '{{\"sizing\":\"{S}\"}}'",
    f"{OP} \"{{\\\"sizing\\\":\\\"{S}\\\"}}\"\ncat > scratch/out.txt <<EOF\nbody\nEOF",
    f"python3 -c \"print('cat > {S} <<EOF')\" > scratch/o.txt",
    f"{OP} '{{\"n\":\"cat > {S} <<EOF\"}}'",
    f"{OP} '{{\"n\":\"cat > {S} <<'\"'\"'EOF'\"'\"'\"}}' | head -3",
    f"python3 -c \"import json;print(json.dumps({{'n':'cat > {S} <<EOF'}}))\" > scratch/o.txt",
    f"cat > scratch/o.txt <<'EOF'\nthe row names {S}\nEOF\n{OP} '{{\"n\":\"{S}\"}}'",
    f"echo \"cat > {S} <<'EOF'\" > scratch/o.txt",
    f"python3 - <<'PY'\nfrom pathlib import Path\nrow = 'reads {S}'\nPath('scratch/o.txt').write_text(row)\nPY",
    f"python3 - <<'PY'\nimport json\nprint(json.dumps({{'sizing': '{S}'}}))\nPY",
    f"cd /repo && python -c \"\n{IMP}\nprint(d('echo x > {S}'))\"",
]

DENIED = [
    f"echo x > {S}",
    f"cat > {S} <<'EOF'\nbody\nEOF",
    f"cat > {S} <<EOF\nbody\nEOF",
    f"cat <<'EOF' > {S}\nbody\nEOF",
    f"cat <<'EOF' | tee {S}\nbody\nEOF",
    f"cat > scratch/o.txt <<'EOF'\nbody\nEOF\necho x > {S}",
    f"{OP} '{{\"n\":\"{S}\"}}' | tee {S}",
    f"{OP} '{{\"n\":\"{S}\"}}' > {S}",
    f"cp scratch/a.yaml {S}",
    f"mv scratch/a.yaml {S}",
    f"{OP} '{{\"sizing\":\"{S}\"}}'\ncat > {S} <<'EOF'\nbody\nEOF",
    f"python3 - <<'PY'\nopen('{S}','w').write('x')\nPY",
    f"python3 - <<'PY'\nfrom pathlib import Path\nPath('{S}').write_text('x')\nPY",
    f"python3 -c \"open('{S}','w').write('x')\"",
    f"FOO=1 coordinator-invoke plan.readiness_verdict '{{\"n\":\"{S}\"}}' > {S}",
    f"cd /repo && python -c \"\n{IMP}\nopen('{S}','w').write('x')\"",
    f"cd /repo && python -c \"\nfrom os import path\nopen('{S}','w').write('x')\"",
    f"cd /repo && python -c \"\n{IMP}\nprint(d('x'))\" > {S}",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_store_mention_next_to_heredoc_is_allowed(cmd):
    assert not _denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_real_store_write_still_denies(cmd):
    assert _denied(cmd)
