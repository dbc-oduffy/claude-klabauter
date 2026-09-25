"""
coordinator_core.ops.tests.test_unclaim_strips_mirror_names — P026-C1, AC1.

Pins `handoff_transition._unclaim`'s two field-lifetime facts:

  - `claimed_by_name` does not survive an unclaim, on BOTH the drop path
    (a live session releasing its own claim, `reaped_from=None`) and the
    reaper's unclaim path (a dead-holder reap, `reaped_from` set) — the same
    `_unclaim` function serves both, so both are exercised here rather than
    assumed identical from one path's coverage.
  - `human_claimant` is box-scoped under PM ruling 2026-08-19
    (one-box-one-human), anchored on `picked_up_by`, not claim-scoped — it
    is untouched by `_unclaim` on either path. The negative pin: an
    orphaned `human_claimant` with no `claimed_by`/`consumed_by` is a real,
    separately-measured condition (AC1b), not one this chunk's code fix
    strips.

Spec backlink: docs/plans/2026-09-07-a-claim-is-written-twice-and-nothing-
compares-them.md, AC1 / P026-C1.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_unclaim_strips_mirror_names.py -q
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

# Declared, not excused: this file spawns a real `git` process because
# locked_rmw (the write path _unclaim routes through) resolves the git
# common dir via a real `git rev-parse` call — no fixture stands in for
# that. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
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

_TEST_SID = "22222222-2222-2222-2222-222222222222"
_REAPER_SID = "33333333-3333-3333-3333-333333333333"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _seed_claimed_handoff(repo: Path, name: str, *, deliverable_id: str) -> Path:
    """A live in_flight/claimed handoff carrying both `claimed_by_name`
    (mirror snapshot, claim-scoped) and `human_claimant` (box-scoped,
    anchored on `picked_up_by`) — the two fields AC1 distinguishes."""
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
        'claimed_by_name: "Stale Peer"\n'
        'human_claimant: "operator@example.com"\n'
        f"deliverable_id: {deliverable_id}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


def test_drop_path_strips_claimed_by_name_and_preserves_human_claimant(tmp_path):
    """A live session releasing its own claim (reaped_from=None): the
    unclaim strips claimed_by_name and leaves human_claimant intact."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "20260101-drop.md", deliverable_id="dlv-drop-000000")

    result = _run(
        {"verb": "unclaim", "handoff_path": str(handoff)}, repo_root=repo / ".git"
    )

    assert result["exit_code"] == 0, result
    assert _fm_field(handoff, "claimed_by_name") is None
    assert _fm_field(handoff, "human_claimant") == '"operator@example.com"'


def test_reaper_path_strips_claimed_by_name_and_preserves_human_claimant(tmp_path):
    """The reaper's unclaim path (reaped_from set, a dead holder's claim
    being reaped): the same strip/preserve pair holds — `_unclaim` is one
    function for both paths, and this pins that both are actually
    exercised rather than one path standing in for the other."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_claimed_handoff(repo, "20260101-reap.md", deliverable_id="dlv-reap-000000")

    result = _run(
        {
            "verb": "unclaim",
            "handoff_path": str(handoff),
            "reaped_from": _REAPER_SID,
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0, result
    assert _fm_field(handoff, "claimed_by_name") is None
    assert _fm_field(handoff, "human_claimant") == '"operator@example.com"'
    # The reaper's own opt-in provenance signal is written — confirms this
    # run actually took the reaper path, not merely a drop with an unused arg.
    assert _fm_field(handoff, "reaped_from_session") is not None
