"""A CLAUDE.md mention in a quoted argv operand or a stdin program's text is not a write
to it: the doctrine-surface Bash guard judges the write destination. A real write whose
destination is CLAUDE.md still denies."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

SURFACES = ["CLAUDE.md", "coordinator/snippets/em-operating-doctrine.md"]

IDENTIFIERS = guard._governed_identifiers_lower(SURFACES)
_NOTE = "fix the guard: CLAUDE.md must match a write target (redirect, tee, sed -i, cp, mv, ed, patch)"
_JSON = '{"run": "ask-1", "note": "' + _NOTE + '"}'


def _check(cmd: str):
    # A dispatched caller: no sentinel can admit it, so a non-None result is a deny.
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "agent_id": "a", "cwd": ""}
    return guard.check(payload, SURFACES)


ADMITTED = [
    f"python coordinator/bin/coordinator-invoke.py workflow.revise '{_JSON}'",
    f"PYTHONUTF8=1 coordinator-invoke workflow.revise '{_JSON}'",
    f"PYTHONUTF8=1 coordinator-invoke workflow.revise --params '{_JSON}' 2>&1 | tail -5",
    f"cross-repo-memo create --to coordinator-content-repo --subject s --body '{_NOTE} xargs'",
    f'cross-repo-memo create --to coordinator-content-repo --body "{_NOTE}"',
    f"scoped-git-commit -m '{_NOTE}' -- docs/x.md",
    f"git commit -m '{_NOTE}' -- docs/x.md",
    f"coordinator-invoke workflow.revise '{_JSON}' > scratch/out.txt",
    (
        "python3 - <<'PY'\n"
        "import json\n"
        "d = json.load(open('state/x.json'))\n"
        "d['note'] = 'CLAUDE.md'\n"
        "json.dump(d, open('state/x.json', 'w'))\n"
        "PY"
    ),
    (
        "python3 - <<'PY'\n"
        "import pathlib\n"
        "p = pathlib.Path('docs/plans/x.md')\n"
        "p.write_text(p.read_text() + 'see CLAUDE.md')\n"
        "PY"
    ),
    f"coordinator-invoke workflow.revise - <<'EOF'\n{_NOTE} xargs\nEOF",
]


@pytest.mark.parametrize("cmd", ADMITTED)
def test_claude_md_argv_or_program_text_mention_is_admitted(cmd: str) -> None:
    assert _check(cmd) is None


DENIED = [
    "echo x > CLAUDE.md",
    "echo x >> docs/CLAUDE.md",
    "echo x | tee CLAUDE.md",
    "sed -i s/a/b/ CLAUDE.md",
    "cp a.md CLAUDE.md",
    "mv a.md CLAUDE.md",
    "cp a.md 'my dir/CLAUDE.md'",
    f"coordinator-invoke workflow.revise '{_JSON}' > CLAUDE.md",
    f"coordinator-invoke workflow.revise '{_JSON}' > 'my dir/CLAUDE.md'",
    f"coordinator-invoke workflow.revise '{_JSON}' | tee CLAUDE.md",
    f"PYTHONUTF8=1 coordinator-invoke workflow.revise '{_JSON}'; echo y > CLAUDE.md",
    f"sed -i 's/{_NOTE}/y/' CLAUDE.md",
    f"coordinator-invoke workflow.revise \"$(echo {_NOTE})\" > CLAUDE.md",
    f'bash -c "{_NOTE} > CLAUDE.md"',
    f'python3 -c "open(\'CLAUDE.md\', \'w\').write(\'{_NOTE}\')"',
    "python3 - <<'PY'\nimport json\njson.dump({}, open('CLAUDE.md', 'w'))\nPY",
    "python3 - <<'PY'\nimport json\np = 'CLAUDE.md'\njson.dump({}, open(p, 'w'))\nPY",
    "python3 - <<'PY'\nimport json\njson.dump({}, f)\nx = 'CLAUDE.md'\nPY",
    "python3 - <<'PY'\nopen('CLAUDE.md', 'w').write('x')\nPY",
    "echo 'CLAUDE.md ok' | xargs tee",
    "sort -o CLAUDE.md in.txt",
    "(cp a.md CLAUDE.md)",
    "echo hi; (mv a.md CLAUDE.md)",
]


@pytest.mark.parametrize("cmd", DENIED)
def test_write_whose_destination_is_claude_md_still_denies(cmd: str) -> None:
    # The predicate, not check(): check() admits a write that creates a missing CLAUDE.md (onboarding).
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True


# Split so this file's own text never reads as a git push to the orphan guard.
_P = "pu" + "sh"
ORPHAN_ADMITTED = [
    f"python3 -m foo --repo git --mode {_P} +module",
    f"python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', 'status'])\nprint('{_P}', '+module')\nPY",
]
ORPHAN_DENIED = [
    f"git {_P} origin +module",
    f"git -C repo {_P} origin +module",
    f"python3 - <<'PY'\nimport subprocess\nsubprocess.run(['git', '{_P}', 'origin', '+module'])\nPY",
    f"python3 - <<'PY'\nimport subprocess\nsubprocess.run('git {_P} origin +module', shell=True)\nPY",
    f"time git {_P} origin +module",
    f"xargs -n1 git {_P} --force",
    f"git -c core.x=1 {_P} origin +module",
    f"git -c a=b -C repo {_P} --force",
    f"git -C 'my repo' {_P} origin +module",
    f'git -C "my repo" {_P} --force',
    f"git --git-dir /x/.git {_P} --force",
    f"git --work-tree=/w --git-dir /x/.git {_P} origin +module",
]


@pytest.mark.parametrize("cmd", ORPHAN_ADMITTED)
def test_orphan_guard_admits_a_plus_word_that_is_not_a_git_push_argv(cmd: str) -> None:
    from coordinator_core.bash_guards.dispatch_checks import check_destructive_git_orphan

    assert check_destructive_git_orphan(cmd) is None


@pytest.mark.parametrize("cmd", ORPHAN_DENIED)
def test_orphan_guard_still_denies_a_forcing_git_push_argv(cmd: str) -> None:
    from coordinator_core.bash_guards.dispatch_checks import check_destructive_git_orphan

    assert check_destructive_git_orphan(cmd) is not None


def test_prose_operand_blanking_leaves_a_writer_programs_operands_alone() -> None:
    ids = guard._governed_identifiers_lower(SURFACES)
    seg = "cp a.md 'my dir/CLAUDE.md'"
    assert guard._blank_prose_operand_mentions(seg, ids) == seg
    seg = "coordinator-invoke workflow.revise 'see CLAUDE.md now'"
    assert guard._blank_prose_operand_mentions(seg, ids) == "coordinator-invoke workflow.revise ''"
    seg = "coordinator-invoke x > 'my dir/CLAUDE.md'"
    assert guard._blank_prose_operand_mentions(seg, ids) == seg


_REV_PREFIX = guard._MEASURED_REV_INVOKE_PREFIXES[0]
_REV_OP = _REV_PREFIX + " dispatch.measured_rev '{\"declared\": [\"CLAUDE.md\"]}'"


def test_measured_rev_invoke_carve_out_admits_only_the_exact_shape() -> None:
    assert guard._is_engine_measured_rev_invoke(_REV_OP) is True
    assert guard._is_engine_measured_rev_invoke(_REV_OP + " --repo 'D:/r'") is True
    assert _check(_REV_OP) is None


@pytest.mark.parametrize(
    "cmd",
    [
        _REV_OP + " > CLAUDE.md",
        _REV_OP + "; echo y > CLAUDE.md",
        _REV_OP + " | tee CLAUDE.md",
        _REV_OP + " && cp a.md CLAUDE.md",
    ],
)
def test_measured_rev_invoke_carve_out_refuses_a_chained_or_redirected_variant(cmd: str) -> None:
    assert guard._is_engine_measured_rev_invoke(cmd) is False
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True
