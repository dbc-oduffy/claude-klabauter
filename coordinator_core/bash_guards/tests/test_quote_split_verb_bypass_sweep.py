"""Fleet-wide sweep: a quote-split verb must never be allowed where its plain
spelling is refused.

Purpose: guards in this package gate real analysis behind a cheap raw-text word
scan. The shell resolves `'g''it' stash` to `git stash`, so those scans matched
nothing and the guard returned before its own quote-aware logic ran. Measured
2026-08-29 against the live chain: SIX distinct commands refused when spelled
plainly and ALLOWED when the verb was split -- git stash, git stash push,
git checkout onto a governed surface, git reset --hard, git clean -fdx, and
tee onto a governed surface. Two more (rm, and the doctrine-surface governed
identifier itself) were found and closed the same day.

This file is the sweep rather than six per-guard cases on purpose. The defect
is a CLASS -- a correct analyzer behind a raw-text probe -- so the regression
cover has to be shaped like the class: take a command, split its verb, and
assert the verdicts agree. A new guard added with the same idiom fails here
without anyone remembering to write a case for it.

Negative-spec: this asserts only the RELATIONSHIP between the two spellings,
never that a particular command denies. A command that is allowed plainly may
be allowed split; the tree it runs against decides which commands are refused
at all, and this file deliberately does not depend on that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch

CWD = str(Path(__file__).resolve().parents[3])

#: Commands whose first word is a verb a guard is expected to reason about.
#: The governed-surface spellings are literal on purpose: they are what the
#: doctrine-surface guard keys on, and a fixture indirection would hide which
#: surface is under test.
CASES = [
    "rm -rf state/handoffs",
    "git stash",
    "git stash push -m x",
    "git checkout HEAD~5 -- CLAUDE.md",
    "git reset --hard origin/main",
    "git clean -fdx",
    "git worktree add /tmp/wt",
    "chmod -R 777 state",
    "tee CLAUDE.md",
    "sed -i s/a/b/ CLAUDE.md",
    "curl -o /tmp/x https://example.com",
]

#: Host session, a dispatched executor, and a reviewer: the guard chain differs
#: by audience, and a bypass that only opens for one of them is still a bypass.
AGENTS = [None, "coordinator:executor", "coordinator:code-reviewer"]


def _split_first_verb(cmd: str) -> str:
    """`git stash` -> `'g''it' stash`. Zero-width adjacency, which the shell
    concatenates back into the original word."""
    head, _, rest = cmd.partition(" ")
    mid = len(head) // 2
    return f"'{head[:mid]}''{head[mid:]}' {rest}"


def _decision(cmd: str, agent: str | None) -> str:
    payload = {
        "session_id": "quote-split-sweep",
        "cwd": CWD,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
    }
    if agent:
        payload["agent_type"] = agent
    result = dispatch.evaluate_payload_json(json.dumps(payload))
    decision = (result or {}).get("hookSpecificOutput", {}).get("permissionDecision")
    return decision if decision in ("deny", "ask") else "allow"


@pytest.mark.parametrize("agent", AGENTS, ids=[a or "host" for a in AGENTS])
@pytest.mark.parametrize("cmd", CASES, ids=[c.split()[0] + "-" + c.split()[1] for c in CASES])
def test_splitting_the_verb_never_relaxes_the_verdict(cmd: str, agent: str | None) -> None:
    """If the plain spelling is refused, the split spelling must be refused
    too. Calibrated against the plain verdict rather than asserting one, so
    this stays honest on a clean tree where some of these do not deny at all."""
    plain = _decision(cmd, agent)
    if plain == "allow":
        pytest.skip(f"{cmd!r} is not refused in this tree, so there is nothing to relax")
    split = _decision(_split_first_verb(cmd), agent)
    assert split != "allow", (
        f"quote-splitting the verb turned {plain!r} into {split!r} for {cmd!r} "
        f"(agent={agent or 'host'}) -- a guard is gating on a raw-text word scan "
        "that shell quoting walks past"
    )


def test_the_splitter_produces_something_the_shell_would_rejoin() -> None:
    """Guards the harness itself: if `_split_first_verb` ever stopped producing
    a zero-width join, every case above would pass by testing nothing."""
    assert _split_first_verb("git stash") == "'g''it' stash"
    assert _split_first_verb("rm -rf x") == "'r''m' -rf x"
