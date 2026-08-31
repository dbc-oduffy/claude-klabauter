"""Unclaiming a blocked baton parks it instead of writing a state it cannot hold.

`_unclaim` stamps `ready_to_fire` unconditionally and never reads `blocked_by`,
so a claimed+blocked node whose holder died aborted on
`_cf_ready_to_fire_no_unresolved_blocked_by`: the claim stood, and
`reap-orphaned-in-flight-handoffs` re-reported rc=1 on it every morning with
nothing able to clear it. It now routes through `_apply_derived_readiness`, the
TIGHTEN-ONLY seam that already parks exactly this shape.

Origin: cross-repo/inbox/2026-08-31-doe-claude-em-reaper-unclaims-blocked-baton-
to-ready-to-fire.md.

Run: python3 -m pytest coordinator_core/ops/tests/test_unclaim_parks_a_blocked_baton.py -q
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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_TEST_SID = "33333333-3333-3333-3333-333333333333"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir(parents=True, exist_ok=True)
    _git(r, "init")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "README.md").write_text("init\n", encoding="utf-8")
    _git(r, "add", "README.md")
    _git(r, "commit", "-m", "init")
    return r


def _seed(repo: Path, name: str, *, blocked_by: Optional[str]) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        "deployment_state: in_flight\n"
        "claimed_at: 2026-01-01T00:00:00Z\n"
        f'claimed_by: "{_TEST_SID}"\n'
    )
    if blocked_by:
        fm += f"blocked_by:\n  - {blocked_by}\n"
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


def _unclaim(repo: Path, rel: str) -> dict:
    return asyncio.run(
        ht._handler({"verb": "unclaim", "handoff_path": rel}, repo_root=repo)
    )


def test_a_blocked_baton_parks_rather_than_failing_validation(repo):
    path = _seed(repo, "blocked.md", blocked_by="hnd-something-else-000000")

    result = _unclaim(repo, "state/handoffs/blocked.md")

    assert result.get("exit_code") == 0, result
    assert _field(path, "status") == "open"
    assert _field(path, "deployment_state") == "awaiting_gate"
    assert _field(path, "pickup_ready") == "false"
    assert _field(path, "claimed_by") is None
    assert "awaiting_gate" in (result.get("message") or "")


def test_an_unblocked_baton_still_reaches_the_shelf(repo):
    path = _seed(repo, "free.md", blocked_by=None)

    result = _unclaim(repo, "state/handoffs/free.md")

    assert result.get("exit_code") == 0, result
    assert _field(path, "status") == "open"
    assert _field(path, "deployment_state") == "ready_to_fire"
    assert _field(path, "claimed_by") is None
