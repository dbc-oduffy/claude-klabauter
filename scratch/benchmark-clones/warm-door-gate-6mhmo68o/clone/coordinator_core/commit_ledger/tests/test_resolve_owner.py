"""Tests for coordinator_core.commit_ledger.resolve_owner.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C3.md

One test per arm (AC2): single held claim, zero held claims (standalone),
multiple held claims (degraded ordering), an agent commit resolved via its
EM's session id, and the agent-back-pointer-missing raise case.

Mirrors the sibling ``baton_assemble`` test fixtures
(``coordinator_core/baton_assemble/tests/test_predecessor_stage_rank_stamped.py``)
rather than inventing a second seed-a-claim-dir helper: a real git repo is
required because ``_resolve_held_handoff_for_session``'s multi-claim leg
resolves ``coordinator_core.session.core.sessions_dir()`` via
``git rev-parse --git-common-dir``, an unavoidable subprocess spawn on that
one path (never for the single-claim/standalone arms, which never reach
``sessions_dir()`` at all).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.commit_ledger import resolve_owner
from coordinator_core.test_baton_assemble import _init_repo

# Declares a real external-process spawn (spawn ratchet Rule 2) -- only the
# multiple-held-claims arm actually spawns git, but the fixture is shared
# across this module's tests for consistency with the sibling suite.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _seed_handoff_claim(
    repo_root: Path,
    session_id: str,
    basename: str,
    claimed_at: str | None = None,
    stage: str | None = None,
) -> Path:
    claims_dir = repo_root / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(session_id, encoding="utf-8")
    if claimed_at is not None:
        (claims_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    if stage is not None:
        (claims_dir / "stage").write_text(stage, encoding="utf-8")
    return claims_dir


def _agent_dir(repo_root: Path, agent_id: str, owner_sid: str) -> Path:
    agent_dir = repo_root / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text(owner_sid + "\n", encoding="utf-8")
    return agent_dir


# ---------------------------------------------------------------------------
# Arm 1: exactly one held claim
# ---------------------------------------------------------------------------


def test_single_held_claim_resolves_normally(tmp_path):
    _init_repo(tmp_path)
    sid = "sid-single"
    _seed_handoff_claim(tmp_path, sid, "2026-08-19-only.md")

    handoff_id, degraded = resolve_owner.resolve_owner_handoff_id(sid, tmp_path)

    assert handoff_id == "2026-08-19-only"
    assert degraded is False


# ---------------------------------------------------------------------------
# Arm 2: zero held claims -- standalone, NOT the raise case
# ---------------------------------------------------------------------------


def test_zero_held_claims_resolves_standalone_none(tmp_path):
    _init_repo(tmp_path)
    sid = "sid-standalone"

    handoff_id, degraded = resolve_owner.resolve_owner_handoff_id(sid, tmp_path)

    assert handoff_id is None
    assert degraded is False


# ---------------------------------------------------------------------------
# Arm 3: multiple held claims -- resolves via the ordering key, degraded=True
# ---------------------------------------------------------------------------


def test_multiple_held_claims_resolves_degraded(tmp_path):
    _init_repo(tmp_path)
    sid = "sid-multi"
    # Neither claim carries claimed_at/stage metadata -- both land on the
    # same sentinel stage_rank/claimed_at ordering legs (see
    # _resolve_held_handoff_for_session's own "degraded" docstring leg: "the
    # set carried no readable claim metadata at all"). The remaining mtime
    # leg falls back to each claim DIR's own st_mtime -- pinned identical via
    # os.utime so this test does not depend on filesystem timing to land the
    # genuine three-way tie that makes `degraded` True.
    alpha = _seed_handoff_claim(tmp_path, sid, "2026-08-19-alpha.md")
    beta = _seed_handoff_claim(tmp_path, sid, "2026-08-19-beta.md")
    tied_mtime = 1_700_000_000.0
    os.utime(alpha, (tied_mtime, tied_mtime))
    os.utime(beta, (tied_mtime, tied_mtime))

    handoff_id, degraded = resolve_owner.resolve_owner_handoff_id(sid, tmp_path)

    assert handoff_id in ("2026-08-19-alpha", "2026-08-19-beta")
    assert degraded is True


# ---------------------------------------------------------------------------
# Arm 4: an agent commit resolves via its dispatching EM's session id
# ---------------------------------------------------------------------------


def test_agent_commit_resolves_via_owning_em_session(tmp_path):
    _init_repo(tmp_path)
    em_sid = "sid-em-owner"
    agent_id = "agent-42"
    _seed_handoff_claim(tmp_path, em_sid, "2026-08-19-em-baton.md")
    _agent_dir(tmp_path, agent_id, em_sid)

    handoff_id, degraded = resolve_owner.resolve_owner_handoff_id(agent_id, tmp_path)

    assert handoff_id == "2026-08-19-em-baton"
    assert degraded is False


# ---------------------------------------------------------------------------
# Arm 5: no agent back-pointer -- the genuinely-unresolvable raise case
# ---------------------------------------------------------------------------


def test_agent_dir_with_missing_back_pointer_raises(tmp_path):
    _init_repo(tmp_path)
    agent_id = "agent-orphaned"
    orphan_dir = tmp_path / ".git" / "coordinator-sessions" / ".agents" / agent_id
    orphan_dir.mkdir(parents=True, exist_ok=True)
    # No em-session-id.txt written -- back-pointer missing.

    with pytest.raises(ValueError, match="back-pointer"):
        resolve_owner.resolve_owner_handoff_id(agent_id, tmp_path)


# ---------------------------------------------------------------------------
# Empty committer_id
# ---------------------------------------------------------------------------


def test_empty_committer_id_raises(tmp_path):
    _init_repo(tmp_path)

    with pytest.raises(ValueError, match="committer_id"):
        resolve_owner.resolve_owner_handoff_id("", tmp_path)
