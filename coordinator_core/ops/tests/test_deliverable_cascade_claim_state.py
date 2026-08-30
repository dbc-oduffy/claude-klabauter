"""
coordinator_core.ops.tests.test_deliverable_cascade_claim_state — C6a1: proves
`deliverable_cascade._claimant` resolves ledger-first, not mirror-only.

Purpose: `deliverable.cascade_terminal`'s leg (a) predicate is the highest-severity
of the seven C6a sites — it TERMINALIZES a candidate handoff (deployment_state ->
shipped) when it (wrongly) concludes nobody holds it. A branch-switch-reverted
frontmatter mirror (the incident `coordinator_core/claim_state.py`'s own module
docstring names, commit 11fe08d51) must never read as "unclaimed" here. This test
seeds exactly that desync — a live claim ledger entry with a reverted (empty)
frontmatter mirror — and proves the candidate is refused, not advanced.

Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
§ Tasks row C6a (this chunk: C6a1, `deliverable_cascade._claimant`).

Negative-spec: does NOT re-test `resolve_claim_state`'s own ledger/mirror
resolution logic (see `coordinator_core/tests/test_claim_state_accessor.py`) — only
that THIS site routes a claim read through it instead of the tracked-frontmatter
mirror directly.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_deliverable_cascade_claim_state.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op side effect
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_handler = cascade_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_LIVE_CLAIMANT_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def _git(
    repo: Path, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    args_list = list(args)
    if (
        len(args_list) >= 3
        and args_list[0] == "commit"
        and args_list[1] == "-m"
        and session_id is not None
        and "Session-Id:" not in args_list[2]
    ):
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {session_id}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_desynced_handoff(
    repo: Path,
    name: str,
    *,
    deliverable_id: str = "dlv-test-000000",
) -> Path:
    """A handoff with a LIVE ledger claim but a reverted (unclaimed) mirror —
    the exact desync `resolve_claim_state`'s module docstring names."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _write_ledger_claim(common_dir: Path, handoff_name: str, session_id: str) -> Path:
    claim_dir = claim_state_mod.handoff_claim_dir(common_dir, Path(handoff_name))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")
    return claim_dir


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _fm_field(path: Path, key: str) -> Optional[str]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field(split.fm_text, key)


def test_desynced_ledger_claim_refuses_not_terminalizes(tmp_path, monkeypatch):
    """A live ledger claim with a reverted mirror must refuse the candidate —
    never flip deployment_state to shipped out from under the claimant."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_desynced_handoff(repo, "20260101-desync.md")

    _write_ledger_claim(repo / ".git", "20260101-desync.md", _LIVE_CLAIMANT_SESSION_ID)

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True), \
        mock.patch.object(
            cascade_mod, "resolve_live_session_ids", return_value={_LIVE_CLAIMANT_SESSION_ID}
        ):
        result = _run(
            {
                "deliverable_id": "dlv-test-000000",
                "source_kind": "plan",
                "source_path": "docs/plans/dummy.md",
            },
            repo_root=repo / ".git",
        )

    assert result["advanced"] == []
    assert result["exit_code"] == 1
    assert len(result["refused"]) == 1
    refusal = result["refused"][0]
    assert refusal["handoff_path"] == str(handoff)
    assert _LIVE_CLAIMANT_SESSION_ID in refusal["reason"]
    assert "claimed by live session" in refusal["reason"]

    # The write this predicate gates never happened — mirror untouched.
    assert _fm_field(handoff, "deployment_state") == "ready_to_fire"


def test_claimant_reads_ledger_not_mirror_directly(tmp_path, monkeypatch):
    """`_claimant` must resolve via `resolve_claim_state` — proven by a mirror
    that carries NO claimed_by/consumed_by field at all still resolving to the
    ledger's live holder."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_desynced_handoff(repo, "20260101-desync2.md", deliverable_id="dlv-test-000001")
    assert _fm_field(handoff, "claimed_by") is None
    assert _fm_field(handoff, "consumed_by") is None

    _write_ledger_claim(repo / ".git", "20260101-desync2.md", _LIVE_CLAIMANT_SESSION_ID)

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True):
        claimant = cascade_mod._claimant(handoff, repo / ".git")

    assert claimant == _LIVE_CLAIMANT_SESSION_ID
