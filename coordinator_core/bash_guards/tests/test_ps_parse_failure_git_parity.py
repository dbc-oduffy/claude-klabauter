"""AC3/AC5/AC10 regression pin: the PowerShell parse-failure route must not
fail OPEN on the destructive-git surface.

Spec backlink: docs/plans/2026-08-19-the-held-guard-cohort-becomes-dialect-safe.md
(AC3 -- `tokens is None` routes to C2's PowerShell-shaped scanner and still
denies on a hit; AC5 -- anchored differential; AC10 -- PowerShell classifier at
parity with the Bash leg; AC12 -- the `MATCHERS` flip is only safe once this
holds).

Why this file exists rather than a case appended to the guard's own suite:
`--` is not valid PowerShell, so `git checkout -- <path>` never reaches the
tokenized path at all. When C3 landed, `_evaluate_powershell_git_destructive`
returned ``None`` for `segments is None`, so those forms ALLOWED under
PowerShell while DENYING under Bash -- a guard reporting as covered while
permitting worktree-destroying commands on a tree shared by 50-70 concurrent
sessions. The whole guard suite was green with that hole open, because every
existing case tokenized cleanly.

Negative-spec: do NOT relax the parse-failure route to `return None`, and do
NOT route it into `_evaluate_git_segment_legacy` (bash-shaped free-text
scanning -- the spurious-deny class this cohort exists to kill).
"""

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.bash_guards._dialect import (
    Dialect,
    resolve_segments_for_dialect,
)

_IDENTITY = {"agent_id": "deadbeef0123", "agent_type": "coordinator:executor"}


@pytest.fixture(autouse=True)
def _wire_subagent_identity(monkeypatch):
    """Seam-patch identity resolution so the DENY path fires without a real
    git repo / back-pointer chain on disk -- the pattern each guard's own
    test file already uses.
    """
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
    monkeypatch.setattr(
        guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
    )
    monkeypatch.setattr(
        guard,
        "_read_backpointer_subagent_type",
        lambda git_root, agent_id, **kw: "coordinator:executor",
    )


def _verdict(command: str, tool_name: str) -> str:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    payload.update(_IDENTITY)
    return "deny" if guard.check(payload) is not None else "allow"


#: Every form here is a real worktree-destroying invocation. Each is asserted
#: to deny under POSIX FIRST (the anchor), then asserted equal across dialects
#: -- a bare ``bool(ps) == bool(posix)`` would pass with both allowing, which
#: is precisely the regression this file pins.
_DESTRUCTIVE = [
    "git checkout -- .",
    "git checkout -- path/to/file",
    "git checkout HEAD -- .",
    "git reset --hard HEAD~1",
    "git push --force origin main",
    "git clean -fdx",
    "git branch -D feature",
    "git restore .",
    "git stash clear",
]

#: Benign, and the last two specifically route through ``tokens is None``.
_BENIGN = [
    "git status",
    "git log --oneline",
    "git diff -- .",
    "echo hello",
]

#: Hazard-documenting prose -- the doe-claude shape the cohort's Problem
#: section names. Must NOT deny: the command is quoted, not issued.
_PROSE = [
    'Write-Output "do not run git reset --hard here"',
    "echo 'git checkout -- . is destructive'",
]


@pytest.mark.parametrize("command", _DESTRUCTIVE)
def test_destructive_git_denies_under_both_dialects(command):
    posix = _verdict(command, "Bash")
    assert posix == "deny", f"anchor failed: {command!r} must deny under Bash"
    assert _verdict(command, "PowerShell") == posix


@pytest.mark.parametrize("command", _BENIGN)
def test_benign_git_allows_under_both_dialects(command):
    posix = _verdict(command, "Bash")
    assert posix == "allow", f"anchor failed: {command!r} must allow under Bash"
    assert _verdict(command, "PowerShell") == posix


@pytest.mark.parametrize("command", _PROSE)
def test_hazard_documenting_prose_does_not_deny(command):
    assert _verdict(command, "PowerShell") == "allow"


def test_pathspec_separator_actually_takes_the_parse_failure_route():
    """Pins the PREMISE, not just the verdict.

    If a future tree-sitter-pwsh upgrade starts parsing `--`, the parity
    assertions above would still pass while silently no longer exercising
    the parse-failure route -- and the fail-open hole could return unnoticed
    on some other unparseable form. This test fails loudly at that point so
    the coverage gap is visible rather than silent.
    """
    assert (
        resolve_segments_for_dialect(
            "git checkout -- .", Dialect.POWERSHELL, guard_name="test"
        )
        is None
    )


def test_parse_failure_route_denies_directly():
    """The scanner itself denies on a hit -- not merely the whole guard."""
    assert guard._evaluate_legacy_powershell_git("git checkout -- .") is not None
    assert guard._evaluate_legacy_powershell_git("git status") is None
