"""Witness corpus for the doctrine-surface Bash guard's read-only class: a command with no write
sink on a governed surface is allowed; a paired genuine write onto the same surface still denies.

Witnesses are recovered from coordinator-content-repo bug-backlog records (open
``state/bug-backlog/2026-10-08-guard-doctrine-surface-bash-write-blocks-0f46e31c4931`` and the
archived ``2026-08-02-...-4c1e90b7a3d2``, ``2026-08-22-doctrine-surface-bash-write-guard-denie``,
``2026-08-14-...-dff03282239e``, ``2026-08-28-...-merely-name-a-governed-file``,
``2026-09-01-...-5afe5af585d7``) and from the example-game-repo fire
``1f3e79fdb07b4ad0bacff318aea4b6c1`` (the Bash tool_use inputs of agent 1, the scope worker whose
final message reports the guard denial). Host paths are rewritten repo-relative.

Not recoverable verbatim, so reconstructed from the record's prose and marked ``reconstructed``:
the ``coordinator-queue-append`` body of the open record (it names no flags), the queue-append
filing of ``4c1e90b7a3d2`` (victim 2), and the awk/sed read of ``dff03282239e`` (the record names
the shape, not the command). The example-game-repo fire journal does not keep the denied command's verdict;
agent 1's two commands that never completed (items 7 and 10) are the candidates, and the engine
at this tree allows both, so they pin the allow side as regressions.

``CLASS_ALLOW``/``CLASS_DENY`` pin the classifier's sink resolution beyond the witnesses.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

SURFACES = [
    "global-doctrine/CLAUDE.md",
    "CLAUDE.md",
    "coordinator/snippets/em-operating-doctrine.md",
    "coordinator/snippets/agent-role-dispatched.md",
    "coordinator.local.md",
]
G = "coordinator/snippets/em-operating-doctrine.md"
SZ = "state/sizings/c6.yaml"


class Witness(NamedTuple):
    id: str
    record: str
    allow: str
    deny: str
    tool: str = "Bash"


_PS = 'pwsh -NoProfile -Command "{}"'

_HOLO_ITEM_10 = (
    'pwsh -NoProfile -Command "Get-Content CLAUDE.md; Get-Content DIRECTORY.md; '
    "Get-ChildItem control -Name; Get-ChildItem gpu_sidecar -Name; Get-ChildItem docs -Name; "
    "Get-Content pyproject.toml; Get-Content control/server/package.json; "
    "Get-Content docs/reference/architecture.md -TotalCount 130; "
    "rg -n -i 'SHeaP|example-retrieval-repo-ue-addon-d1' research_notes docs state "
    "scratch/warp/pipeline-research-20261122T145344-9ab17c -g '*.md' -m 3\""
)
_HOLO_ITEM_8 = (
    'pwsh -NoProfile -Command "Get-Content README.md; Get-ChildItem -Name; '
    "rg --files -g '*CONTEXT.md' -g '*CLAUDE.md' -g '*AGENTS.md' -g '.cursorrules' "
    "-g '\"'!scratch/**'\"' -g '\"'!node_modules/**'\"' -g '\"'!vendor/**'\"'\""
)

WITNESSES = [
    Witness(
        "open-0f46-pm-brief-reads-sizing",
        "2026-10-08-...-0f46e31c4931",
        f"python3 coordinator/bin/pm-brief.py from-sizing ./{SZ}",
        f"python3 coordinator/bin/pm-brief.py from-sizing ./{SZ} > ./{SZ}",
    ),
    Witness(
        "open-0f46-cp-from-sizing",
        "2026-10-08-...-0f46e31c4931",
        "cp state/sizings/x.yaml scratch/",
        "cp scratch/x.yaml state/sizings/x.yaml",
    ),
    Witness(  # reconstructed
        "open-0f46-queue-append-body-names-dir",
        "2026-10-08-...-0f46e31c4931",
        'coordinator-queue-append --body "the guard refuses any command that names state/sizings/ even as a read"',
        'coordinator-queue-append --body "names state/sizings/" && echo x >> state/sizings/x.yaml',
    ),
    Witness(
        "4c1e-one-line-governed-commit",
        "2026-08-02-...-4c1e90b7a3d2",
        f"git add global-doctrine/CLAUDE.md {G} && git commit -q -- global-doctrine/CLAUDE.md {G} "
        "-F - <<'MSG' fix the guard MSG",
        f"git add global-doctrine/CLAUDE.md {G} && git commit -q -- global-doctrine/CLAUDE.md {G} "
        "-F - <<'MSG' fix the guard MSG && echo x >> global-doctrine/CLAUDE.md",
    ),
    Witness(  # reconstructed
        "4c1e-queue-append-quotes-governed-name",
        "2026-08-02-...-4c1e90b7a3d2",
        "coordinator-queue-append --target state/bug-backlog --body "
        '"a single-line heredoc that names CLAUDE.md is denied"',
        'coordinator-queue-append --body "names CLAUDE.md" && echo x >> CLAUDE.md',
    ),
    Witness(
        "2026-08-22-dotclaude-grep-c",
        "2026-08-22-doctrine-surface-bash-write-guard-denie",
        'grep -c "zzz" docs/plans/2026-07-08-install-baton-rendezvous-off-dotclaude.md',
        'grep -c "zzz" docs/plans/2026-07-08-install-baton-rendezvous-off-dotclaude.md > CLAUDE.md',
    ),
    Witness(  # reconstructed
        "dff0-awk-frontmatter-read",
        "2026-08-14-...-dff03282239e",
        f"awk '/^---$/{{n++}} n==1' {G}",
        f"awk '/^---$/{{n++}} n==1' {G} > {G}",
    ),
    Witness(  # reconstructed
        "dff0-sed-n-frontmatter-read",
        "2026-08-14-...-dff03282239e",
        f"sed -n '1,/^---$/p' {G}",
        f"sed -i '1d' {G}",
    ),
    Witness(
        "2026-08-28-python-c-reads-governed-writes-scratch",
        "2026-08-28-...-merely-name-a-governed-file",
        f"python -c \"\nimport pathlib\np = '{G}'\n"
        "print(len(pathlib.Path(p).read_text(encoding='utf-8')))\n"
        "pathlib.Path('scratch/probe.txt').write_text('x')\n\"",
        f"python -c \"\nimport pathlib\np = '{G}'\n"
        f"pathlib.Path(p).write_text('x')\n\"",
    ),
    Witness(
        "2026-08-28-minimal-pair-variable-then-scratch-write",
        "2026-08-28-...-merely-name-a-governed-file",
        f"p={G} ; cat $p ; echo x > scratch/probe.txt",
        f"p={G} ; echo corrupted > $p",
    ),
    Witness(
        "2026-09-01-suffix-decoy-scratch-write",
        "2026-09-01-...-5afe5af585d7",
        "echo probe > scratch/CLAUDE.md.probe",
        "echo probe > CLAUDE.md",
    ),
    Witness(
        "2026-09-01-filing-heredoc-quotes-governed-name",
        "2026-09-01-...-5afe5af585d7",
        "cat > scratch/bug.yaml <<'EOF'\nbody: the guard blocks CLAUDE.md edits\nEOF",
        "cat > CLAUDE.md <<'EOF'\nbody: the guard blocks CLAUDE.md edits\nEOF",
    ),
    Witness(
        "2026-09-01-pure-grep-read",
        "2026-09-01-...-5afe5af585d7",
        "grep -n pattern global-doctrine/CLAUDE.md",
        "grep -n pattern global-doctrine/CLAUDE.md > global-doctrine/CLAUDE.md",
    ),
    Witness(
        "example-game-repo-1f3e-agent1-item10-get-content-claude-md",
        "example-game-repo fire 1f3e79fdb07b4ad0bacff318aea4b6c1",
        _HOLO_ITEM_10,
        _PS.format("Get-Content README.md > CLAUDE.md"),
        tool="PowerShell",
    ),
    Witness(
        "example-game-repo-1f3e-agent1-item8-rg-files-claude-md-glob",
        "example-game-repo fire 1f3e79fdb07b4ad0bacff318aea4b6c1",
        _HOLO_ITEM_8,
        _PS.format("Get-Content README.md > CLAUDE.md"),
        tool="PowerShell",
    ),
]

CLASS_ALLOW = [
    "cat CLAUDE.md | head -5 > scratch/o",
    "grep -rn foo CLAUDE.md | xargs grep bar",
    "sed -n '1,5p' CLAUDE.md > scratch/o",
    "sed -i s/a/b/ scratch/x.txt; cat CLAUDE.md",
    "bash -c 'cat CLAUDE.md > scratch/o'",
    "python3 scripts/x.py --out scratch/o CLAUDE.md",
    "python3 scripts/x.py from-sizing CLAUDE.md --output=scratch/o",
    "echo $(cat CLAUDE.md | wc -l) > scratch/n",
    "cp CLAUDE.md scratch/CLAUDE.copy",
    "cd scratch && cat ../CLAUDE.md",
    'pwsh -NoProfile -Command "Get-Content CLAUDE.md | Select-String foo"',
]
CLASS_DENY = [
    "cat x &> CLAUDE.md",
    "cat x >& CLAUDE.md",
    "cat CLAUDE.md & cp x CLAUDE.md",
    "cat x | tee CLAUDE.md",
    "sed -i s/a/b/ CLAUDE.md",
    "sed -i -e s/a/b/ CLAUDE.md",
    "cp x CLAUDE.md",
    "mv x CLAUDE.md",
    "cp x .\\CLAUDE.md",
    "bash -c 'echo x > CLAUDE.md'",
    "eval 'echo x > CLAUDE.md'",
    "echo CLAUDE.md | xargs sed -i s/a/b/",
    "echo CLAUDE.md | xargs tee",
    "echo $(echo x > CLAUDE.md)",
    "cat `echo CLAUDE.md`; echo x > CLAUDE.md",
    "python3 scripts/x.py --out CLAUDE.md",
    "python3 scripts/x.py --output=CLAUDE.md",
    "python3 scripts/x.py -o CLAUDE.md",
    "python3 scripts/x.py --in-place CLAUDE.md",
    "python3 scripts/x.py CLAUDE.md",
    "cd global-doctrine && echo x > CLAUDE.md",
    f"cd state && echo x > ../{G}",
    "echo x > ./CLAUDE.md.",
    "echo x > scratch/../CLAUDE.md",
    'pwsh -NoProfile -Command "Set-Content CLAUDE.md x"',
    'pwsh -NoProfile -Command "Get-Content a > CLAUDE.md"',
    "(cd x; echo y > CLAUDE.md)",
    "awk '{print > \"CLAUDE.md\"}' x",
    "python3 -c \"open('CLAUDE.md','w').write('x')\"",
    "f=CLAUDE.md; echo x > $f",
    "f=CLAUDE.md; f=a; echo x > $f",
    "echo x > 'CLAU''DE.md'",
    "cat x > scratch/o; echo y>CLAUDE.md",
    "xargs -I{} sh -c 'echo {} > CLAUDE.md'",
    "case x in x) cp y CLAUDE.md;; esac",
    "sort -o CLAUDE.md x",
    "sed -n 'w CLAUDE.md' x",
]


@pytest.fixture(autouse=True)
def _gate_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", raising=False)
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()


def _repo(root: Path) -> Path:
    """A cwd where every governed surface exists, so a write is never the onboarding creation."""
    for rel in SURFACES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# x\n", encoding="utf-8")
    return root


def _verdict(cmd: str, tool: str, cwd: Path):
    cwd = _repo(cwd)
    payload = {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(cwd)}
    return guard.check(payload, list(SURFACES))


@pytest.mark.parametrize("w", [pytest.param(w, id=w.id) for w in WITNESSES])
def test_witness_with_no_governed_write_sink_is_allowed(w: Witness, tmp_path: Path) -> None:
    assert _verdict(w.allow, w.tool, tmp_path) is None, w.allow


@pytest.mark.parametrize("w", [pytest.param(w, id=w.id) for w in WITNESSES])
def test_paired_genuine_write_onto_the_surface_still_denies(w: Witness, tmp_path: Path) -> None:
    assert _verdict(w.deny, w.tool, tmp_path) is not None, w.deny


@pytest.mark.parametrize("cmd", CLASS_ALLOW)
def test_class_read_only_command_is_allowed(cmd: str, tmp_path: Path) -> None:
    assert _verdict(cmd, "Bash", tmp_path) is None, cmd


@pytest.mark.parametrize("cmd", CLASS_DENY)
def test_class_write_sink_on_the_surface_still_denies(cmd: str, tmp_path: Path) -> None:
    assert _verdict(cmd, "Bash", tmp_path) is not None, cmd
