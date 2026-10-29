"""`coordinator-invoke.exe plan.readiness_verdict '<json>'` whose JSON names a
state/sizings path and carries `->` arrows and `'"'"'` splices is a mention, not
a write; a real redirect, tee or cat into the store still denies."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import check

S = "state/sizings/2026-10-28-x-2a6e2d.yaml"
EXE = "C:/Users/example-operator/.coordinator-claude-settings/bin/coordinator-invoke.exe"
JSON = (
    '{"plan":"p.md","premise":{"rows":[{"citation":"' + S + '",'
    '"evidence":"git rev-parse --verify 6649fb4d11 -> 6649fb4d1184 | wc -l -> 6 '
    "grep -c '\"'\"'purpose'\"'\"' f.py -> 0\"}]}}"
)
JSON_Q = JSON.replace("'", "'\"'\"'")


def _denied(cmd: str) -> bool:
    return check({"tool_name": "Bash", "tool_input": {"command": cmd}}, ["CLAUDE.md"]) is not None


ALLOWED = [
    f"cd E:/example-retrieval-repo && {EXE} plan.readiness_verdict '{JSON_Q}'",
    f"{EXE} plan.readiness_verdict '{JSON_Q}'",
    f"coordinator-invoke plan.readiness_verdict '{JSON_Q}'",
    f"{EXE} sizing.record_pm_resolution '{JSON_Q}'",
    f"cd E:/example-retrieval-repo && coordinator-invoke plan.other_op '{JSON_Q}' 2>&1",
    f"cat <<'EOF'\nsee {S} -> then > done\nEOF",
    f"cat <<'EOF' | wc -l\nsee CLAUDE.md for details > ok\nEOF",
    f'echo "see {S} and CLAUDE.md here"',
    f'echo "see {S}" > scratch/a.txt',
    f"{EXE} plan.other_op '{{\"p\":\"CLAUDE.md -> x\"}}'",
]

DENIED = [
    f"{EXE} plan.other_op '{JSON_Q}' > {S}",
    f"{EXE} plan.other_op '{{\"p\":\"CLAUDE.md\"}}' > CLAUDE.md",
    "cat > CLAUDE.md <<'EOF'\nbody\nEOF",
    f"{EXE} plan.readiness_verdict '{JSON_Q}' > {S}",
    f"{EXE} plan.readiness_verdict '{JSON_Q}' | tee {S}",
    f"cat > {S} <<'EOF'\nbody\nEOF",
    f"echo x > {S}",
    f"cd E:/example-retrieval-repo && {EXE} plan.readiness_verdict '{JSON_Q}' > {S}",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_readiness_verdict_json_mention_is_admitted(cmd):
    assert not _denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_real_store_write_still_denies(cmd):
    assert _denied(cmd)
