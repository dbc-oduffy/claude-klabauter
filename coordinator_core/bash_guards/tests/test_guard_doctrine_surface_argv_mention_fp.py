"""A governed path as an argv operand of a read-only or ungoverned-write command is
not a governed write; the sizing-record leg of the doctrine-surface Bash guard judges the write
target: a redirect outside state/sizings does not deny a command that only
names a sizing as an argument, and a write into state/sizings still denies."""

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


def _check(cmd: str):
    return _enveloped(guard.check)({"tool_name": "Bash", "tool_input": {"command": cmd}}, None)


@pytest.mark.parametrize(
    "cmd",
    [
        "cat state/sizings/a.yaml > scratch/out.txt",
        "grep intent state/sizings/a.yaml >> scratch/out.txt",
        "git diff state/sizings/a.yaml > scratch/d.patch",
        "wc -l state/sizings/a.yaml 2>/dev/null > scratch/n",
        'head -5 state/sizings/a.yaml > "scratch/out file.txt"',
    ],
)
def test_argument_mention_with_scratch_redirect_is_allowed(cmd):
    assert _check(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "cat x > state/sizings/a.yaml",
        "cat scratch/a >> state/sizings/a.yaml",
        "cat state/sizings/a.yaml > scratch/o && cat y > state/sizings/b.yaml",
        "cat state/sizings/a.yaml > scratch/o; sed -i s/a/b/ state/sizings/a.yaml",
        "cat state/sizings/a.yaml > scratch/o; cp scratch/o state/sizings/a.yaml",
        "cat state/sizings/a.yaml | tee state/sizings/b.yaml > scratch/o",
        'cat x > "state/sizings/a.yaml"',
        "python3 tool.py state/sizings/a.yaml > scratch/o",
        "cat state/sizings/a.yaml > $OUT",
        "cat state/sizings/a.yaml > scratch/o <<EOF",
    ],
)
def test_write_into_sizings_still_advises(cmd):
    result = _check(cmd)
    assert result is not None
    # guard-doctrine-surface-bash-write is ADVISORY_BY_RULING (b17ec35251; DR-475, 307ea22403):
    # a sizings write is advised, never denied.
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "sizing store" in result["hookSpecificOutput"]["additionalContext"]

IDS = guard._SIZING_RECORD_IDENTIFIERS
SZ = "state/sizings/2026-10-11-x.yaml"
DOC = "coordinator/docs/wiki/some-page.md"

ALLOW_CASES = [
    (
        "accept-exit-criterion with a read-only command substitution",
        f'sizing-accept-exit-criterion --sizing {SZ} --apm-ruling "$(cat f)" --criterion x',
    ),
    (
        "python -c scratch write joined with accept-exit-criterion",
        f"python3 -c \"open('scratch/o.txt','w').write('x')\" && "
        f"sizing-accept-exit-criterion --sizing {SZ}",
    ),
    (
        "sizing-assemble intent quoting a governed snippet path",
        f'sizing-assemble --intent "edit {DOC} and state/sizings/z.yaml notes" --write {SZ}',
    ),
]

DENY_CASES = [
    ("echo of a substitution redirected onto a sizing", f'echo "$(cat f)" > {SZ}'),
    (
        "python -c writing a sizing joined with accept call",
        f"python3 -c \"open('{SZ}','w').write('x')\" && "
        f"sizing-accept-exit-criterion --sizing {SZ}",
    ),
    (
        "stdin program binding a governed path reaching a write",
        f"python3 - <<'PY'\np='{SZ}'\nopen(p,'w').write('x')\nPY",
    ),
]


SZY = "state/sizings/x.yaml"
SZ_GOV = ("state/sizings/",)

# DR-394: git add/commit operands are data; a quoted interpreter word is prose.
GIT_ALLOW = [
    f"git commit -m 'run python3 halted' -- {SZY}",
    f"git add -- {SZY}",
    f"git add -- {SZY} state/warp/r/ && git commit -m 'run python3 halted' -- {SZY}",
]
GIT_DENY = [
    f'git commit -m "$(python3 -c 1)" -- {SZY}',
    f'git commit -m "`python3 x`" -- {SZY}',
    f"git -c core.editor='python3 e.py' commit -- {SZY}",
    f"GIT_EDITOR='python3 e.py' git commit -- {SZY}",
    f"echo x > {SZY}",
    f"git checkout HEAD -- {SZY}",
    f"git log --grep 'python3 -c' -- {SZY}",
]


@pytest.mark.parametrize("cmd", GIT_ALLOW)
def test_git_add_commit_quoted_interpreter_allowed(cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, SZ_GOV) is False


@pytest.mark.parametrize("cmd", GIT_DENY)
def test_git_live_execution_and_writes_still_denied(cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, SZ_GOV) is True


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_argv_mention_allows(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDS) is False, label


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_real_write_still_denies(label: str, cmd: str) -> None:
    assert guard.is_denied_bash_write(cmd, IDS) is True, label
