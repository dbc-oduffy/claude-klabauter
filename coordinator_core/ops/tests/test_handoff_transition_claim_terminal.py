"""
coordinator_core.ops.tests.test_handoff_transition_claim_terminal — a finished
baton is not re-pickable.

Purpose: pins `handoff_transition._claim`'s terminal-deployment_state refusal.
Claim was the one verb in that module carrying no terminality precondition, so
a `shipped`/`continued`/`closed` handoff was silently re-armed to `in_flight`
by any pickup touch and stayed perpetually re-pickable (reproduced upstream as
one baton claimed three times in a single day). The refusal must be loud
(exit_code=1, an `error` naming the state) and must leave the record byte-
unchanged, and the membership test must read the shared
`lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT` set rather than a local
literal.

The non-terminal cases below are the load-bearing half: this is the pickup hot
path, and a gate that also refuses a `ready_to_fire` baton is worse than the
bug it fixes.

Negative-spec: does NOT re-test claim's session_id fail-loud, its stale-claim
takeover re-stamp, or unclaim's own (pre-existing, already-correct) terminal
precondition — that one is asserted here only as the sibling case proving the
release path was never the hole.

Run (from repo root):
    python -m pytest coordinator_core/ops/tests/test_handoff_transition_claim_terminal.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.handoff_transition as ht
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns a real `git` process because
# locked_rmw (the write path _claim routes through) resolves the git common
# dir via a real `git rev-parse` call — no fixture stands in for that. Spawn
# ratchet: coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_handler = ht._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_HOLDER_SID = "22222222-2222-2222-2222-222222222222"
_CLAIMANT_SID = "33333333-3333-3333-3333-333333333333"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


# Review: overengineering-reviewer -- the seed repo was rebuilt per test case
# (~50 git spawns for 13 cases) to exercise an in-process precondition that
# touches no git state; `_seed` already keys each test's handoff by a distinct
# filename, so one repo shared across the module preserves isolation at 4
# spawns total instead of 4 per case.
@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("repo")
    _git(path, "init")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("init\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")
    return path


def _seed(
    repo: Path,
    name: str,
    *,
    deployment_state: str,
    status: str,
    holder: Optional[str] = _HOLDER_SID,
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    if holder is not None:
        fm += f'claimed_at: 2026-01-01T00:00:00Z\nclaimed_by: "{holder}"\n'
    if deployment_state == "continued":
        fm += 'continued_into: "state/handoffs/20260102-successor.md"\n'
    elif deployment_state == "closed":
        fm += "closed_reason: cancelled\n"
    elif deployment_state == "shipped":
        fm += 'shipped_in: "0000000000000000000000000000000000000000"\n'
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _run(params: dict, repo: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def _claim(handoff: Path, repo: Path, sid: str = _CLAIMANT_SID) -> dict:
    return _run(
        {
            "verb": "claim",
            "handoff_path": str(handoff),
            "session_id": sid,
            "at": "2026-09-02T00:00:00Z",
        },
        repo,
    )


def _unclaim(handoff: Path, repo: Path) -> dict:
    return _run({"verb": "unclaim", "handoff_path": str(handoff)}, repo)


def _field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


# ---------------------------------------------------------------------------
# Terminal batons refuse both transitions.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("deployment_state", sorted(HANDOFF_TERMINAL_DEPLOYMENT))
def test_terminal_baton_refuses_claim(repo: Path, deployment_state: str) -> None:
    handoff = _seed(
        repo,
        f"20260101-{deployment_state}.md",
        deployment_state=deployment_state,
        status="claimed",
    )
    before = handoff.read_bytes()

    result = _claim(handoff, repo)

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert deployment_state in result["error"]
    assert "terminal" in result["error"]
    assert handoff.read_bytes() == before


# Review: coordinator:code-reviewer — HANDOFF_TERMINAL_DEPLOYMENT is used here
# only as a convenience sample of non-{in_flight,ready_to_fire} values; unlike
# _claim above, _unclaim's check is a positive allowlist unrelated to the
# terminal set, and did NOT change in this diff. Each of these 4 values is
# rejected for the same reason any other non-member value would be.
@pytest.mark.parametrize("deployment_state", sorted(HANDOFF_TERMINAL_DEPLOYMENT))
def test_terminal_baton_refuses_unclaim(repo: Path, deployment_state: str) -> None:
    handoff = _seed(
        repo,
        f"20260101-{deployment_state}-unclaim.md",
        deployment_state=deployment_state,
        status="claimed",
    )
    before = handoff.read_bytes()

    result = _unclaim(handoff, repo)

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert deployment_state in result["error"]
    assert handoff.read_bytes() == before


def test_terminal_baton_stays_terminal_across_repeat_pickups(repo: Path) -> None:
    """The reported symptom, end to end: repeated pickup touches on a
    `continued` baton must never leave it re-pickable."""
    handoff = _seed(
        repo,
        "20260101-repeat.md",
        deployment_state="continued",
        status="claimed",
    )

    for _ in range(3):
        assert _claim(handoff, repo)["exit_code"] == 1
        assert _unclaim(handoff, repo)["exit_code"] == 1

    assert _field(handoff, "deployment_state") == "continued"
    assert _field(handoff, "pickup_ready") is None


# ---------------------------------------------------------------------------
# The hot path is untouched — non-terminal batons still claim and unclaim.
# ---------------------------------------------------------------------------


def test_ready_to_fire_baton_still_claims(repo: Path) -> None:
    handoff = _seed(
        repo,
        "20260101-ready.md",
        deployment_state="ready_to_fire",
        status="open",
        holder=None,
    )

    result = _claim(handoff, repo)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert _field(handoff, "status") == "claimed"
    assert _field(handoff, "deployment_state") == "in_flight"
    assert _CLAIMANT_SID in (_field(handoff, "claimed_by") or "")


def test_in_flight_baton_still_unclaims(repo: Path) -> None:
    handoff = _seed(
        repo,
        "20260101-inflight.md",
        deployment_state="in_flight",
        status="claimed",
    )

    result = _unclaim(handoff, repo)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert _field(handoff, "status") == "open"
    assert _field(handoff, "deployment_state") == "ready_to_fire"
    assert _field(handoff, "claimed_by") is None


def test_claim_unclaim_reclaim_round_trip_is_unchanged(repo: Path) -> None:
    handoff = _seed(
        repo,
        "20260101-roundtrip.md",
        deployment_state="ready_to_fire",
        status="open",
        holder=None,
    )

    assert _claim(handoff, repo)["exit_code"] == 0
    assert _unclaim(handoff, repo)["exit_code"] == 0
    assert _claim(handoff, repo)["exit_code"] == 0

    assert _field(handoff, "status") == "claimed"
    assert _field(handoff, "deployment_state") == "in_flight"


def test_claim_idempotency_no_op_survives_the_gate(repo: Path) -> None:
    """The terminal gate is raised before the D5 idempotency check; an
    already-claimed+in_flight record held by THIS session must still no-op
    rather than be caught by it."""
    handoff = _seed(
        repo,
        "20260101-idem.md",
        deployment_state="ready_to_fire",
        status="open",
        holder=None,
    )
    assert _claim(handoff, repo)["exit_code"] == 0
    before = handoff.read_bytes()

    result = _claim(handoff, repo)

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert "no-op" in result["message"]
    assert handoff.read_bytes() == before
