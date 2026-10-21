"""A state/sizings path named as a commit CLI/op argument is not a shell write
into it; every real write sink into state/sizings still denies, chained or not."""
import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _sizing_write_denied,
)

S = "state/sizings/2026-10-21-x.yaml"
INVOKE = "python3 coordinator/bin/coordinator-invoke.py ceremony.commit_v2"
SAFE = "python3 coordinator/bin/coordinator-safe-commit.py"

ALLOWED = [
    f'{SAFE} --body-file F "subj" -- docs/plans/x.md {S}',
    f'{SAFE} "subj" -- {S}',
    f'cd /r && {SAFE} -m "s" --body-file b -- docs/p.md {S} && echo ok',
    f'{SAFE} "s" -- {S} 2>&1',
    f"coordinator-safe-commit.py --body-file F s -- {S}",
    f"PYTHONPATH=. {SAFE} s -- {S}",
    f"coordinator-invoke ceremony.commit_v2 '{{\"paths\":[\"{S}\"]}}'",
    f"{INVOKE} '{{\"paths\":[\"docs/p.md\",\"{S}\"],\"message\":\"a > b\"}}'",
    f"{INVOKE} '{{\"paths\":[\"{S}\"]}}' 2>&1 | tail -3",
    f"{INVOKE} - <<'EOF'\n{{\"paths\":[\"{S}\"]}}\nEOF",
    f"git status && {INVOKE} '{{\"paths\":[\"{S}\"]}}'",
]

DENIED = [
    f"echo x > {S}",
    f"echo x >> {S}",
    f"printf x | tee {S}",
    f"sed -i 's/a/b/' {S}",
    f"cp /tmp/a.yaml {S}",
    f"mv /tmp/a.yaml {S}",
    f"cat > {S} <<'EOF'\nk: v\nEOF",
    f'{SAFE} "s" -- {S} > {S}',
    f'{SAFE} "s" -- docs/p.md; echo x > {S}',
    f'{SAFE} "s" -- {S} && sed -i s/a/b/ {S}',
    f'{SAFE} "s" -- {S} && cp /tmp/a {S}',
    f"{INVOKE} '{{\"paths\":[\"{S}\"]}}' && echo x > {S}",
    f'{SAFE} "s" -- {S} && tee {S} < /tmp/a',
    f'{SAFE} "$(echo hi > {S})" -- docs/p.md',
    f"{SAFE} s -- {S} && cat <<'EOF' > {S}\nk: v\nEOF",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_commit_argument_mention_is_allowed(cmd):
    assert not _sizing_write_denied(cmd)


@pytest.mark.parametrize("cmd", DENIED)
def test_shell_write_into_sizings_still_denies(cmd):
    assert _sizing_write_denied(cmd)
