"""
coordinator_core.tests.test_archive_stamp_human_claimant — C8 coverage for the
`human_claimant` row of `archive_stamp._record_claimant_identity_best_effort`
(folded 2026-08-30 from the former standalone
`_record_human_claimant_best_effort` — Review: overengineering-reviewer
(Kira), same lock-and-RMW pass as `claimed_by_name`/`claimed_by_address`
now), the claim-time stamp of the OPERATING HUMAN (via
`resolve_operating_person().get("github")`) written beside
`picked_up_by`/`claimed_by` on both the memo claim path
(`cs_claim_memo_stamp`) and the handoff claim path (`cs_claim_handoff`).

PM ruling, 2026-08-19 (one-box-one-human): the claiming session resolves the
operating human at the moment it claims — authored, never derived, never
resolved inside a sweep. This module pins:

  1. `cs_claim_memo_stamp` writes `human_claimant` carrying C1's resolved
     slug, additive beside the unchanged SESSION-id-carrying `picked_up_by`.
  2. `cs_claim_handoff` writes `human_claimant` on its own claim path too.
  3. A record claimed BEFORE this shipped (no `human_claimant` in its
     frontmatter, simulating a pre-C8 record) gets no backfill from an
     unrelated write — absent stays absent under the frozen contract.
  4. An unresolvable operating human (empty `resolve_operating_person()`
     bundle) omits the key entirely — no sentinel, no `"unknown"` fallback
     (the explicit NAME COLLISION warning against
     `machine_resolver.compute_contributor` in this chunk's brief).

Spec backlink: state/dispatch-briefs/2026-08-19-the-tracker-names-an-owner/C8.md
Negative-spec: does NOT import `coordinator_core.machine_resolver.compute_contributor` —
that resolver is a differently-derived, differently-shaped "contributor slug"
(env var / machine-registry / email-derived, with an `"unknown"` fallback)
that is NOT this axis's value space.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

import coordinator_core.ops.handoff_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.memo_transition  # noqa: F401 — @register_op side effect
import coordinator_core.ops.session.record_pickup  # noqa: F401 — @register_op side effect

import coordinator_core.archive_stamp as arstamp
from coordinator_core.tests._fixtures import init_repo as _init_repo
from coordinator_core.tests._fixtures import run_git as _git

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_DEFAULT_TEST_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def _seed_memo(repo: Path, name: str, status: str, extra: str = "") -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "kind: fyi\n"
        f"status: {status}\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_handoff(repo: Path, name: str, status: str, deployment_state: str, extra: str = "") -> Path:
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
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


# ---------------------------------------------------------------------------
# Memo claim path — cs_claim_memo_stamp
# ---------------------------------------------------------------------------


class TestMemoClaimHumanClaimant:
    def test_claim_stamps_human_claimant_beside_unchanged_picked_up_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m1.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {"github": "octocat"})

        rc = arstamp.cs_claim_memo_stamp(str(mp))

        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        # picked_up_by keeps carrying the SESSION id, unchanged.
        assert "picked_up_by: sess-abc" in text
        # human_claimant is additive, carrying C1's resolved slug.
        assert "human_claimant: octocat" in text

    def test_unresolvable_operating_human_omits_key_entirely(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(repo, "m2.md", "open")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        # Fully unresolvable bundle — resolve_operating_person's own documented
        # "empty dict means fully unresolvable" contract.
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {})

        rc = arstamp.cs_claim_memo_stamp(str(mp))

        assert rc == 0
        text = mp.read_text(encoding="utf-8")
        assert "picked_up_by: sess-abc" in text
        # No sentinel, no "unknown" fallback — the key is simply absent.
        assert "human_claimant" not in text

    def test_preexisting_claim_without_human_claimant_is_not_backfilled(self, tmp_path, monkeypatch):
        """A record claimed BEFORE this shipped — already in_progress, already
        carrying picked_up_by/at, no human_claimant — must NOT get one
        backfilled by an unrelated later write. This exercises release only:
        it proves release doesn't mint human_claimant onto a pre-existing
        record it didn't itself write. It does not cover re-claim after
        release; whether re-claim then stamps a *new* human_claimant where
        none existed before is a separate, untested path."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(
            repo, "m3.md", "in_progress",
            extra="picked_up_at: '2026-01-01T00:00:00Z'\npicked_up_by: sess-old\n",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-new")
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {"github": "newperson"})

        # Release must not retroactively mint human_claimant onto the
        # pre-existing record it didn't itself write.
        rc_release = arstamp.cs_release_memo_revert(str(mp))
        assert rc_release == 0
        assert "human_claimant" not in mp.read_text(encoding="utf-8")

    def test_idempotent_reclaim_does_not_overwrite_existing_human_claimant(self, tmp_path, monkeypatch):
        """A claim record already carrying human_claimant is left untouched,
        never re-stamped or overwritten (docstring contract)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        mp = _seed_memo(
            repo, "m4.md", "open",
        )
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-abc")
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {"github": "first-claimant"})
        rc1 = arstamp.cs_claim_memo_stamp(str(mp))
        assert rc1 == 0
        assert "human_claimant: first-claimant" in mp.read_text(encoding="utf-8")

        # Release and re-claim as a different resolved human — the existing
        # human_claimant value must not have been overwritten by the first
        # write path being invoked twice inside one claim.
        arstamp.cs_release_memo_revert(str(mp))
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {"github": "second-claimant"})
        rc2 = arstamp.cs_claim_memo_stamp(str(mp))
        assert rc2 == 0
        text = mp.read_text(encoding="utf-8")
        # Release strips picked_up_by/at but is documented not to touch
        # human_claimant at all (this chunk adds no release-side behavior) —
        # so the re-claim's insert-only-when-absent guard sees it still set
        # and leaves the original value in place.
        assert "human_claimant: first-claimant" in text
        assert "second-claimant" not in text


# ---------------------------------------------------------------------------
# Handoff claim path — cs_claim_handoff
# ---------------------------------------------------------------------------


class TestHandoffClaimHumanClaimant:
    def test_claim_handoff_stamps_human_claimant_beside_unchanged_claimed_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h1.md", "open", "active")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-h1")
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {"github": "hcat"})

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-h1" in text
        assert "human_claimant: hcat" in text

    def test_claim_handoff_unresolvable_human_omits_key(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(repo, "h2.md", "open", "active")
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-h2")
        monkeypatch.setattr(arstamp, "resolve_operating_person", lambda: {})

        rc = arstamp.cs_claim_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "claimed_by: sess-h2" in text
        assert "human_claimant" not in text
