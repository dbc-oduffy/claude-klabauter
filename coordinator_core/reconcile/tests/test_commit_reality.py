"""
coordinator_core.reconcile.tests.test_commit_reality — DEC-1 matcher fixtures.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2

Covers the plan's required scenario matrix against a real tmp git repo:
  - clear-ship -> auto-ship
  - deliverable-absent -> surface
  - commit-but-no-deliverable -> surface
  - no-commit -> no-match
  - mechanical-commit-only (pickup: subject, no real deliverable commit) -> no-match
  - cross-handoff-scope-overlap (two open handoffs share a scope path, one matching
    commit -> BOTH surface, zero auto-ship)

Also covers the 2026-07-20 claude-central-em false-positive memo, Defect 2:
  - directory-only scope entries contribute no subject-match tokens
  - the >=2-distinct-token subject-match threshold
  - directory-only `_deliverable_present` returns False under the file-required default
  - the attribution count only credits file-level (non-directory) pathspec overlap
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core.reconcile.commit_reality import (
    _derive_noun_tokens,
    _deliverable_present,
    _discriminating_pathspecs,
    _is_cross_repo_scope_entry,
    _is_directory_scope_disk_aware,
    _is_mechanical_subject,
    _pathspec_overlaps,
    _split_cross_repo_scope,
    _subject_matches_tokens,
    evaluate_commit_reality,
)

#: Evidence substring emitted ONLY by `_evaluate_explicit_ship_claim`'s
#: self-scope-overlap branch (`commit_reality.py:780`) -- positively proves a
#: test exercised the explicit-ship-claim path under test, rather than
#: reaching `auto-ship` via the ordinary signal-(a) subject-match candidate
#: path (code-review F1: a vocabulary-overlap accident previously let a test
#: in this suite pass through the wrong path with zero coverage of the
#: function it was named for).
_EXPLICIT_SHIP_CLAIM_SCOPE_OVERLAP_MARKER = (
    "explicit shipped_in SHA verified reachable and touches own scope"
)

# Declared, not excused: this file's `_git` helper and `repo` fixture spawn real git
# because the property under test IS real git-history matching -- reachability
# (`git rev-parse`/merge-base-style checks in `_evaluate_explicit_ship_claim`),
# Session-Id trailer provenance attribution off real commit messages, and real
# scope-overlap against actual committed paths, none of which a mock can stand in
# for without reimplementing the git plumbing being tested. `repo` stays
# function-scoped (default) since many tests build distinct, test-specific commit
# histories that must not leak between tests sharing a repo. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the route
# for this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DEFAULT_POLICY = {
    "three_signal": {},
    "mechanical_commit_denylist": [
        "pickup:",
        "reclaim(docs)",
        "session-init",
        "memo:",
        "handoff.transition",
    ],
    "cross_handoff_attribution": True,
    "dry_run": True,
}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tmp git repo with identity configured, ready for commits."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    # git log --since needs at least one commit to anchor history.
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: seed repo")
    return root


def _commit_file(
    root: Path, rel_path: str, content: str, subject: str, session_id: str = ""
) -> str:
    """Write + commit a file under rel_path with the given commit subject; return sha.

    `session_id`, when non-empty, appends a `Session-Id: <session_id>` trailer
    (as a second paragraph) to the commit message -- mirrors
    `coordinator_core/test_archive_stamp.py`'s `_git()` trailer convention, used
    by `TestExplicitShipClaimProvenanceAttribution` below to seed commits
    attributed to a specific (own or peer) session.
    """
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    _git(root, "add", rel_path)
    message = f"{subject}\n\nSession-Id: {session_id}" if session_id else subject
    _git(root, "commit", "-q", "-m", message)
    result = _git(root, "rev-parse", "HEAD")
    return result.stdout.strip()


def _handoff(handoff_id: str, scope: List[str], title: str) -> dict:
    return {
        "id": handoff_id,
        "scope": scope,
        "title": title,
        "created": "2020-01-01",
    }


class TestClearShip:
    def test_clear_ship_auto_ships(self, repo: Path) -> None:
        sha = _commit_file(
            repo,
            "widget_engine/core.py",
            "print('widget engine')\n",
            "feat: land widget engine core",
        )
        handoff = _handoff("h-widget", ["widget_engine/core.py"], "Widget Engine")

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha
        assert result["confidence"] == "high"
        assert result["handoff_id"] == "h-widget"


class TestDeliverableAbsent:
    def test_deliverable_absent_surfaces(self, repo: Path) -> None:
        # Commit created the deliverable, matching signal (a); a later commit removes
        # it from disk, so signal (b) fails while the (now-stale) commit is still a
        # valid subject-match candidate under the same scope pathspec.
        sha = _commit_file(
            repo,
            "flywheel_module/core.py",
            "print('flywheel module')\n",
            "feat: land flywheel module core",
        )
        (repo / "flywheel_module" / "core.py").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "pickup: remove stale file")

        handoff = _handoff("h-flywheel", ["flywheel_module/core.py"], "Flywheel Module")

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == sha


class TestCommitButNoDeliverable:
    def test_commit_without_deliverable_surfaces(self, repo: Path) -> None:
        sha = _commit_file(
            repo,
            "gizmo_module/core.py",
            "print('gizmo module')\n",
            "feat: land gizmo module",
        )
        # Scope path itself is later removed from disk (deliverable absent) but the
        # commit still exists under history for that path. The removal commit uses a
        # denylisted mechanical subject so it is never itself picked as the candidate.
        (repo / "gizmo_module" / "core.py").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "pickup: remove stale file")

        handoff = _handoff("h-gizmo", ["gizmo_module/core.py"], "Gizmo Module")

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == sha


class TestNoCommit:
    def test_no_commit_is_no_match(self, repo: Path) -> None:
        handoff = _handoff("h-phantom", ["phantom_module/core.py"], "Phantom Module")

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "no-match"
        assert result["candidate_sha"] is None


class TestMechanicalCommitOnly:
    def test_mechanical_only_commit_is_no_match(self, repo: Path) -> None:
        _commit_file(
            repo,
            "sprocket_tool/core.py",
            "print('sprocket tool')\n",
            "pickup: touch sprocket tool scope",
        )
        handoff = _handoff("h-sprocket", ["sprocket_tool/core.py"], "Sprocket Tool")

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "no-match"


class TestMechanicalDenylistPrefixNotSubstring:
    """Slice-A review Finding 1: every denylist entry except the
    `handoff.transition`-family marker matches as a PREFIX, not a substring.
    A real subject that merely CONTAINS a denylist token (not as a prefix)
    must NOT be denylisted."""

    def test_subject_containing_but_not_prefixed_by_memo_token_is_not_denylisted(
        self,
    ) -> None:
        # "memo:" appears mid-subject, not as a prefix — must not match.
        assert not _is_mechanical_subject(
            "feat: land handoff-memo: rendering fix",
            _DEFAULT_POLICY["mechanical_commit_denylist"],
        )

    def test_subject_containing_but_not_prefixed_by_pickup_token_is_not_denylisted(
        self,
    ) -> None:
        assert not _is_mechanical_subject(
            "fix: repair pickup: field validation on ingest",
            _DEFAULT_POLICY["mechanical_commit_denylist"],
        )

    def test_subject_prefixed_by_denylist_token_is_still_denylisted(self) -> None:
        assert _is_mechanical_subject(
            "memo: send cross-repo brief", _DEFAULT_POLICY["mechanical_commit_denylist"]
        )

    def test_handoff_transition_family_still_matches_as_substring(self) -> None:
        # The one documented substring exception — must still match mid-subject.
        assert _is_mechanical_subject(
            "chore: handoff.transition: ship h-123",
            _DEFAULT_POLICY["mechanical_commit_denylist"],
        )


def _write_plan(root: Path, rel_path: str, status: str) -> None:
    """Write a minimal docs/plans/*.md fixture with a frontmatter `status:` field
    (used by _find_plan_path_in_scope / _read_plan_status corroboration signal)."""
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\nstatus: {status}\n---\n\n# Plan\n", encoding="utf-8")


class TestExplicitShipClaimShippedIn:
    """C3 (F1/F3a) — the shipped_in explicit-ship-claim path (C2), unit-level:
    reachable+deliverable+self-scope-overlap -> auto-ship; each individual
    guard's failure mode -> surface, never a silent no-match (there IS an
    explicit claim, it just doesn't clear verification)."""

    def test_shipped_in_reachable_deliverable_present_overlap_auto_ships(
        self, repo: Path
    ) -> None:
        sha = _commit_file(
            repo,
            "stranded_module/core.py",
            "print('stranded module')\n",
            "memo: seed stranded module (mechanical, never token-matched)",
        )
        handoff = {
            "id": "h-stranded",
            "scope": ["stranded_module/core.py"],
            "title": "Stranded Module Rollout",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha
        assert result["confidence"] == "high"

    def test_shipped_in_sha_not_reachable_surfaces(self, repo: Path) -> None:
        (repo / "unreached_module").mkdir(parents=True, exist_ok=True)
        (repo / "unreached_module" / "core.py").write_text("x\n", encoding="utf-8")
        handoff = {
            "id": "h-unreached",
            "scope": ["unreached_module/core.py"],
            "title": "Unreached Module",
            "created": "2020-01-01",
            "shipped_in": "f" * 40,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == "f" * 40
        assert any("not reachable on HEAD" in e for e in result["evidence"])

    def test_shipped_in_reachable_but_deliverable_absent_surfaces(
        self, repo: Path
    ) -> None:
        sha = _commit_file(
            repo,
            "vanished_module/core.py",
            "print('vanished module')\n",
            "memo: seed vanished module",
        )
        (repo / "vanished_module" / "core.py").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "pickup: remove vanished module")

        handoff = {
            "id": "h-vanished",
            "scope": ["vanished_module/core.py"],
            "title": "Vanished Module",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert any("deliverable absent" in e for e in result["evidence"])

    def test_shipped_in_reachable_deliverable_present_but_scope_disjoint_surfaces(
        self, repo: Path
    ) -> None:
        """F1/F3a load-bearing negative: a reachable SHA whose deliverable happens
        to exist on disk but whose commit touched a totally DIFFERENT set of
        files than this handoff's own scope (e.g. a pasted/copied SHA from a
        sibling handoff) must never auto-ship on reachability+presence alone."""
        (repo / "own_scope_module").mkdir(parents=True, exist_ok=True)
        (repo / "own_scope_module" / "core.py").write_text("own\n", encoding="utf-8")
        _git(repo, "add", "own_scope_module/core.py")
        _git(repo, "commit", "-q", "-m", "memo: seed own scope module (mechanical)")

        unrelated_sha = _commit_file(
            repo,
            "unrelated_module/core.py",
            "print('unrelated module')\n",
            "feat: land totally unrelated module",
        )

        handoff = {
            "id": "h-own-scope",
            "scope": ["own_scope_module/core.py"],
            "title": "Own Scope Module",
            "created": "2020-01-01",
            "shipped_in": unrelated_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == unrelated_sha
        assert any(
            "touches nothing in this handoff's scope" in e for e in result["evidence"]
        )


class TestShippedInKindDiscrimination:
    """DR-096 AC26 — `_evaluate_explicit_ship_claim` must discriminate on
    `shipped_in_kind` BEFORE any git dereferencing: only `ship-commit` and
    `successor` are eligible for the reachability+scope-overlap evaluation
    that can reach confidence:high/verdict:auto-ship. `scope-derived`,
    UNTAGGED, and any unrecognized kind must never read as CLEAR, even when
    the raw `shipped_in` value is a real, reachable, scope-overlapping SHA
    (docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md § AC26)."""

    def _ship_commit(self, repo: Path) -> str:
        return _commit_file(
            repo,
            "kind_gated_module/core.py",
            "print('kind gated module')\n",
            "memo: seed kind gated module (mechanical)",
        )

    def test_ship_commit_kind_reads_clear(self, repo: Path) -> None:
        sha = self._ship_commit(repo)
        handoff = {
            "id": "h-ship-commit-kind",
            "scope": ["kind_gated_module/core.py"],
            "title": "Kind Gated Module",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["confidence"] == "high"
        assert result["candidate_sha"] == sha

    def test_scope_derived_kind_never_reads_clear(self, repo: Path) -> None:
        # Same reachable, scope-overlapping SHA as the ship-commit-kind case
        # above -- only the kind tag differs. A scope-derived value is
        # frequently an unrelated bystander commit by construction (DR-096);
        # it must never promote to auto-ship regardless of how clean the
        # evidence otherwise looks.
        sha = self._ship_commit(repo)
        handoff = {
            "id": "h-scope-derived-kind",
            "scope": ["kind_gated_module/core.py"],
            "title": "Kind Gated Module",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "scope-derived",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"
        assert result["confidence"] != "high"
        assert any("scope-derived" in e for e in result["evidence"])

    def test_untagged_shipped_in_with_valid_sha_never_reads_clear(
        self, repo: Path
    ) -> None:
        # The falsifying case: a perfectly valid-looking, reachable,
        # scope-overlapping SHA with NO shipped_in_kind at all. Pre-AC26
        # behavior promoted this straight to auto-ship; post-AC26 it must
        # never read as CLEAR.
        sha = self._ship_commit(repo)
        handoff = {
            "id": "h-untagged-kind",
            "scope": ["kind_gated_module/core.py"],
            "title": "Kind Gated Module",
            "created": "2020-01-01",
            "shipped_in": sha,
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"
        assert result["confidence"] != "high"
        assert any("no shipped_in_kind tag" in e for e in result["evidence"])

    def test_unrecognized_kind_fails_closed(self, repo: Path) -> None:
        sha = self._ship_commit(repo)
        handoff = {
            "id": "h-bogus-kind",
            "scope": ["kind_gated_module/core.py"],
            "title": "Kind Gated Module",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "definitely-not-a-real-kind",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"
        assert result["confidence"] != "high"
        assert any("not a recognized DR-096 enum value" in e for e in result["evidence"])

    def test_no_commit_kind_never_reads_clear_and_is_not_dereferenced(
        self, repo: Path
    ) -> None:
        # "no-commit" tokens are not SHAs at all -- must short-circuit before
        # any git subprocess runs on the value.
        handoff = {
            "id": "h-no-commit-kind",
            "scope": ["kind_gated_module/core.py"],
            "title": "Kind Gated Module",
            "created": "2020-01-01",
            "shipped_in": "substantively-shipped-no-commit 2026-07-01",
            "shipped_in_kind": "no-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"
        assert result["confidence"] != "high"
        assert any("no-commit" in e for e in result["evidence"])


_OWN_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_PEER_SESSION_ID = "22222222-2222-2222-2222-222222222222"


class TestExplicitShipClaimProvenanceAttribution:
    """2026-07-26 ruling (sidecar finding 4): the top tier (confidence:high /
    verdict:auto-ship) for an explicit `shipped_in` claim requires EITHER
    scope-overlap (unchanged) OR positive Session-Id-trailer provenance
    attribution to the CALLING session -- not scope-overlap alone. Everything
    that meets neither degrades to confidence:partial / verdict:surface, never
    a hard failure. Also guards the laundering case (sidecar finding 4's core
    concern, and the 2026-07-22 incident `force=` exists to repair): a
    reachable, non-scope-touching SHA belonging to a DIFFERENT (peer) session
    must still surface, not auto-ship, even though it is a real commit
    present in the local clone."""

    def test_scope_overlap_alone_still_auto_ships_with_no_session_env_set(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No CLAUDE_SESSION_ID/CLAUDE_CODE_SESSION_ID set at all -- provenance
        # is entirely unresolvable, yet scope-overlap alone must still be
        # sufficient for auto-ship (the OR's first branch is unchanged).
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        sha = _commit_file(
            repo,
            "own_scope_touch/core.py",
            "print('own scope touch')\n",
            "memo: seed own scope touch (mechanical)",
        )
        handoff = {
            "id": "h-scope-overlap",
            "scope": ["own_scope_touch/core.py"],
            "title": "Own Scope Touch",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha
        assert result["confidence"] == "high"

    def test_provenance_attributed_non_scope_touching_sha_auto_ships(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", _OWN_SESSION_ID)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        # Seed the handoff's own scope so it exists on disk (deliverable
        # present), but the ship commit itself touches a totally different
        # path -- scope-overlap will NOT hold.
        (repo / "attributed_module").mkdir(parents=True, exist_ok=True)
        (repo / "attributed_module" / "core.py").write_text("own\n", encoding="utf-8")
        _git(repo, "add", "attributed_module/core.py")
        _git(repo, "commit", "-q", "-m", "memo: seed attributed module scope (mechanical)")

        ship_sha = _commit_file(
            repo,
            "unrelated_ceremony_touch/core.py",
            "print('ceremonial close touch')\n",
            "feat: land an unrelated ceremonial-close commit",
            session_id=_OWN_SESSION_ID,
        )

        handoff = {
            "id": "h-attributed",
            "scope": ["attributed_module/core.py"],
            "title": "Attributed Module",
            "created": "2020-01-01",
            "shipped_in": ship_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == ship_sha
        assert result["confidence"] == "high"
        assert any("Session-Id" in e for e in result["evidence"])

    def test_unattributed_non_scope_touching_sha_degrades_to_surface_not_failure(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_SESSION_ID", _OWN_SESSION_ID)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        (repo / "unattributed_scope").mkdir(parents=True, exist_ok=True)
        (repo / "unattributed_scope" / "core.py").write_text("own\n", encoding="utf-8")
        _git(repo, "add", "unattributed_scope/core.py")
        _git(repo, "commit", "-q", "-m", "memo: seed unattributed scope (mechanical)")

        # No Session-Id trailer at all on the candidate commit.
        ship_sha = _commit_file(
            repo,
            "no_trailer_touch/core.py",
            "print('no trailer touch')\n",
            "feat: land a commit with no Session-Id trailer",
        )

        handoff = {
            "id": "h-unattributed",
            "scope": ["unattributed_scope/core.py"],
            "title": "Unattributed Scope",
            "created": "2020-01-01",
            "shipped_in": ship_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == ship_sha
        assert any(
            "touches nothing in this handoff's scope" in e for e in result["evidence"]
        )

    def test_laundered_peer_session_sha_still_surfaces_not_auto_ship(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard for the 2026-07-22 incident: a real, reachable
        commit stamped from a DIFFERENT (sibling) session's Session-Id must
        not auto-ship onto a baton it has nothing to do with, even under the
        new provenance OR-branch."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", _OWN_SESSION_ID)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        (repo / "victim_scope").mkdir(parents=True, exist_ok=True)
        (repo / "victim_scope" / "core.py").write_text("own\n", encoding="utf-8")
        _git(repo, "add", "victim_scope/core.py")
        _git(repo, "commit", "-q", "-m", "memo: seed victim scope (mechanical)")

        peer_sha = _commit_file(
            repo,
            "sibling_module/core.py",
            "print('sibling session work')\n",
            "feat: land a sibling session's unrelated commit",
            session_id=_PEER_SESSION_ID,
        )

        handoff = {
            "id": "h-victim",
            "scope": ["victim_scope/core.py"],
            "title": "Victim Scope",
            "created": "2020-01-01",
            "shipped_in": peer_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == peer_sha
        assert any(
            "touches nothing in this handoff's scope" in e for e in result["evidence"]
        )


class TestMixedScopeDirectoryEntryGrantsUnearnedHighConfidence:
    """Pins a defect in `_pathspec_overlaps` (:479): it strips a scope entry's
    trailing "/" before prefix-matching, so a DIRECTORY scope entry is
    satisfied by ANY commit touching anything beneath that directory --
    carrying no discriminating power. The arm is only reachable once
    `_deliverable_present` holds, which requires an actual FILE on disk
    (:415), so a directory-only scope alone never reaches it. The exposed
    shape is a MIXED scope: one FILE entry (satisfies deliverable-present)
    plus one DIRECTORY entry (trivially satisfies overlap) -- and the two
    need not be related. A ship commit that never touches the named file, and
    carries no session attribution, must not be credited with `confidence:
    high` / `verdict: auto-ship` on the strength of the directory entry
    alone: honest breadth (touching *something* under a broad directory) is
    not evidence of shipping the deliverable this handoff actually names."""

    def test_mixed_scope_directory_entry_alone_grants_high_confidence(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        # The named FILE entry exists on disk (deliverable_present == True)
        # but is never touched by the ship commit below.
        (repo / "named_module").mkdir(parents=True, exist_ok=True)
        (repo / "named_module" / "core.py").write_text("named\n", encoding="utf-8")
        _git(repo, "add", "named_module/core.py")
        _git(repo, "commit", "-q", "-m", "memo: seed named module deliverable (mechanical)")

        # The ship commit touches only a file UNDER the directory scope
        # entry -- a path the handoff never names -- and carries no
        # Session-Id trailer, so the provenance OR-branch cannot fire either.
        ship_sha = _commit_file(
            repo,
            "shared_dir/unrelated_file.py",
            "print('unrelated file under shared dir')\n",
            "feat: land an unrelated file under the shared directory",
        )

        handoff = {
            "id": "h-mixed-scope",
            "scope": ["named_module/core.py", "shared_dir/"],
            "title": "Mixed Scope Directory Overlap",
            "created": "2020-01-01",
            "shipped_in": ship_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"
        assert result["confidence"] != "high"
        # Pin the directory-specific evidence branch (code-review F2) — a
        # regression reaching the right verdict via the generic no-overlap
        # branch instead would otherwise pass unnoticed.
        assert any(
            "directory-shaped scope entry" in e for e in result["evidence"]
        )

    def test_file_shaped_scope_overlap_alone_still_auto_ships(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 control: the directory-entry fix above must not be collateral
        damage to genuine file-shaped scope evidence. A handoff whose `scope`
        names only specific FILE paths, ship-commit-touched, with no session
        attribution available, must still clear the top tier — the fix
        degrades directory-only overlap, not discriminating overlap.

        Vocabulary note (code-review F1): title/scope-basename-derived tokens
        and the ship-commit subject share ZERO tokens on purpose. A prior
        draft of this test used "File Scope Only Control" / "feat: land the
        file-shaped scope deliverable", which shares the derived tokens
        "file"/"scope" with the subject — enough to clear the 2-token
        `_DEFAULT_SUBJECT_MATCH_MIN_TOKENS` bar via ordinary signal-(a)
        subject matching, so `evaluate_commit_reality` never reached the
        `not candidates` branch and `_evaluate_explicit_ship_claim` — the
        function this test is named for — was never called. This vocabulary
        is verified zero-overlap; the positive-path assertion below also
        proves it structurally, independent of vocabulary hygiene.
        """
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        ship_sha = _commit_file(
            repo,
            "widget_area/beacon.py",
            "print('beacon touch')\n",
            "feat: land a completely disjoint payload",
        )

        handoff = {
            "id": "h-file-scope-control",
            "scope": ["widget_area/beacon.py"],
            "title": "Beacon Deployment Record",
            "created": "2020-01-01",
            "shipped_in": ship_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["confidence"] == "high"
        assert result["candidate_sha"] == ship_sha
        # Positively prove the explicit-ship-claim path was exercised, not
        # just that vocabulary hygiene held (code-review F1 hardening).
        assert any(
            _EXPLICIT_SHIP_CLAIM_SCOPE_OVERLAP_MARKER in e for e in result["evidence"]
        )

    def test_mixed_scope_directory_entry_without_trailing_slash_grants_no_high_confidence(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_is_directory_scope` (:190) only recognizes the trailing-"/"
        convention, but real scope entries in this corpus name directories
        WITHOUT one (`docs/plans`, `state/handoffs`,
        `coordinator/templates/bin`). Absent a disk-aware check, such an entry
        is treated as file-shaped, passes `_discriminating_pathspecs`
        untouched, and `_pathspec_overlaps` then grants a directory-prefix
        match the top tier -- exactly the non-discriminating evidence this
        class's first test already pins for the trailing-slash form. This
        pins the no-trailing-slash form.

        Vocabulary note: `_derive_noun_tokens` (:220) also calls the
        syntactic-only `_is_directory_scope` and is deliberately NOT touched
        by this fix (it has its own callers -- noun-token derivation and the
        cross-handoff attribution count -- broadening it would move all three
        behaviours at once, per the fix brief). So a no-trailing-slash
        directory entry's basename still contributes noun tokens today. The
        title/scope/subject vocabulary below is chosen with ZERO token
        overlap with each other, so the ship commit never matches the DEC-1
        primary subject-token signal (a) and this test genuinely exercises
        the `_evaluate_explicit_ship_claim` self-overlap gate under test,
        rather than an unrelated primary-signal match."""
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        # The named FILE entry exists on disk (deliverable_present == True)
        # but is never touched by the ship commit below.
        (repo / "named_module").mkdir(parents=True, exist_ok=True)
        (repo / "named_module" / "core.py").write_text("named\n", encoding="utf-8")
        _git(repo, "add", "named_module/core.py")
        _git(repo, "commit", "-q", "-m", "memo: seed named module deliverable (mechanical)")

        # `auxscope` exists on disk as a real directory but the scope entry
        # below deliberately omits the trailing slash.
        ship_sha = _commit_file(
            repo,
            "auxscope/unrelated.py",
            "print('unrelated file under an auxiliary directory')\n",
            "feat: touch a path under the incidental area",
        )

        handoff = {
            "id": "h-mixed-scope-no-slash",
            "scope": ["named_module/core.py", "auxscope"],
            "title": "Regression Coverage Without Trailing Punctuation",
            "created": "2020-01-01",
            "shipped_in": ship_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"
        assert result["confidence"] != "high"
        # Pin the directory-specific evidence branch (code-review F2) — see
        # the trailing-slash sibling test above for the reasoning.
        assert any(
            "directory-shaped scope entry" in e for e in result["evidence"]
        )

    def test_extensionless_file_scope_entry_still_grants_high_confidence(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression pin for the rejected fix approach: an extension-less
        pathspec is not necessarily a directory. A scope entry naming a real
        extension-less FILE on disk (mirrors `bin/coordinator-auto-push` in
        the live corpus), ship-commit-touched at that exact path, with no
        session attribution available, must still clear the top tier -- the
        disk-aware directory check must resolve this via `Path.is_dir()`
        (False for a file) and not via an extension heuristic, which would
        wrongly treat this file as directory-shaped and demote it."""
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        ship_sha = _commit_file(
            repo,
            "bin/coordinator-auto-push",
            "#!/usr/bin/env bash\necho auto-push\n",
            "feat: land the extension-less deliverable",
        )

        handoff = {
            "id": "h-extensionless-file-scope",
            "scope": ["bin/coordinator-auto-push"],
            "title": "Extensionless File Scope Control",
            "created": "2020-01-01",
            "shipped_in": ship_sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["confidence"] == "high"
        assert result["candidate_sha"] == ship_sha
        # Positively prove the explicit-ship-claim path was exercised
        # (code-review F1 hardening — see the AC3 control test above).
        assert any(
            _EXPLICIT_SHIP_CLAIM_SCOPE_OVERLAP_MARKER in e for e in result["evidence"]
        )


class TestIsDirectoryScopeDiskAwareRobustness:
    """Code-review F3/F4: `_is_directory_scope_disk_aware` must degrade to the
    syntactic answer rather than raising or probing outside `worktree_root`."""

    def test_embedded_null_byte_degrades_to_syntactic_answer(
        self, tmp_path: Path
    ) -> None:
        # A scope string with an embedded null byte raises ValueError from
        # Path.is_dir(), not OSError -- must degrade, not propagate.
        assert _is_directory_scope_disk_aware("mal\x00formed", tmp_path) is False

    def test_absolute_scope_entry_does_not_probe_outside_worktree(
        self, tmp_path: Path
    ) -> None:
        # tmp_path's own parent is a real, absolute, existing directory with
        # no trailing slash in its string form -- guaranteed cross-platform
        # (Windows and POSIX both), unlike a hardcoded "/etc". Without the
        # confinement guard, pathlib's "/" operator would discard
        # worktree_root entirely (the right operand is absolute) and
        # `.is_dir()` would probe this real directory, wrongly returning
        # True. Confinement means it must fall back to the syntactic
        # (non-directory) answer instead.
        outside_dir = str(tmp_path.parent)
        assert _is_directory_scope_disk_aware(outside_dir, tmp_path) is False

    def test_dotdot_scope_entry_does_not_resolve_upward(self, tmp_path: Path) -> None:
        # No trailing slash, so the syntactic `_is_directory_scope` check
        # does not short-circuit -- this exercises the confinement guard.
        # A worktree_root's own parent is virtually guaranteed to be a real
        # directory -- if the "..".-bearing entry were probed on disk, this
        # would (wrongly) return True. Confinement means it must not be.
        assert _is_directory_scope_disk_aware("nested/../..", tmp_path) is False
        assert _is_directory_scope_disk_aware("field/../../etc", tmp_path) is False


class TestExplicitShipClaimPlanImplemented:
    """C3 — the linked-plan status:implemented corroboration signal (C2 sub-signal
    ii): a plan stamp alone (no reachable self-scope-overlapping SHA) must never
    auto-ship; it must corroborate a shipped_in SHA that independently clears the
    same reachable+deliverable+self-scope-overlap bar."""

    def test_plan_implemented_with_verified_shipped_in_auto_ships(
        self, repo: Path
    ) -> None:
        sha = _commit_file(
            repo,
            "plan_backed_module/core.py",
            "print('plan backed module')\n",
            "memo: seed plan backed module (mechanical)",
        )
        _write_plan(repo, "docs/plans/2026-01-01-plan-backed.md", "implemented")

        handoff = {
            "id": "h-plan-backed",
            "scope": [
                "plan_backed_module/core.py",
                "docs/plans/2026-01-01-plan-backed.md",
            ],
            "title": "Plan Backed Module",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha
        assert any("status:implemented" in e for e in result["evidence"])

    def test_plan_implemented_alone_with_no_shipped_in_surfaces(
        self, repo: Path
    ) -> None:
        _write_plan(repo, "docs/plans/2026-01-01-plan-only.md", "implemented")

        handoff = {
            "id": "h-plan-only",
            "scope": ["docs/plans/2026-01-01-plan-only.md"],
            "title": "Plan Only",
            "created": "2020-01-01",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"

    def test_plan_approved_not_implemented_and_no_shipped_in_is_no_match(
        self, repo: Path
    ) -> None:
        _write_plan(repo, "docs/plans/2026-01-01-plan-approved.md", "approved")

        handoff = {
            "id": "h-plan-approved",
            "scope": ["docs/plans/2026-01-01-plan-approved.md"],
            "title": "Plan Approved",
            "created": "2020-01-01",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "no-match"


class TestExplicitShipClaimPlanLanded:
    """C2 (AC8) — `landed` is a distinct, WEAKER corroboration tier than
    `implemented`, added to `_evaluate_explicit_ship_claim`'s no-`shipped_in`
    branch: a handoff linked to a `landed` plan with no `shipped_in` must
    surface for reconciliation (verdict:surface) rather than returning None
    (silently dropped) — the AC8 defect this chunk fixes. The tier must also
    stay distinguishable from `implemented`, not merged into it (this plan's
    Key decision / AC6): `plan_implemented` is never widened to include
    `landed`."""

    def test_plan_landed_alone_with_no_shipped_in_surfaces(self, repo: Path) -> None:
        _write_plan(repo, "docs/plans/2026-01-01-plan-landed.md", "landed")

        handoff = {
            "id": "h-plan-landed",
            "scope": ["docs/plans/2026-01-01-plan-landed.md"],
            "title": "Plan Landed",
            "created": "2020-01-01",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        # Pre-fix, this returned None from `_evaluate_explicit_ship_claim`
        # (no shipped_in, plan_implemented False) and fell through to the
        # generic no-candidates no-match verdict -- a landed-but-unshipped
        # handoff was silently not surfaced. Post-fix it must surface.
        assert result["verdict"] == "surface"
        assert any("status:landed" in e for e in result["evidence"])

    def test_landed_evidence_text_is_distinguishable_from_implemented(
        self, repo: Path
    ) -> None:
        """Landed and implemented must not share the same evidence string --
        proves the two tiers are genuinely distinct code paths, not one
        condition reading two status values the same way."""
        _write_plan(repo, "docs/plans/2026-01-01-plan-landed-2.md", "landed")
        handoff_landed = {
            "id": "h-plan-landed-2",
            "scope": ["docs/plans/2026-01-01-plan-landed-2.md"],
            "title": "Plan Landed Two",
            "created": "2020-01-01",
        }
        result_landed = evaluate_commit_reality(
            handoff_landed, repo, _DEFAULT_POLICY, []
        )

        _write_plan(repo, "docs/plans/2026-01-01-plan-implemented-2.md", "implemented")
        handoff_implemented = {
            "id": "h-plan-implemented-2",
            "scope": ["docs/plans/2026-01-01-plan-implemented-2.md"],
            "title": "Plan Implemented Two",
            "created": "2020-01-01",
        }
        result_implemented = evaluate_commit_reality(
            handoff_implemented, repo, _DEFAULT_POLICY, []
        )

        landed_evidence = " ".join(result_landed["evidence"])
        implemented_evidence = " ".join(result_implemented["evidence"])
        assert "status:landed" in landed_evidence
        assert "status:implemented" not in landed_evidence
        assert "status:implemented" in implemented_evidence
        assert "status:landed" not in implemented_evidence
        # Both surface (neither has a shipped_in), but via distinct evidence.
        assert result_landed["verdict"] == "surface"
        assert result_implemented["verdict"] == "surface"

    def test_plan_landed_with_verified_shipped_in_still_auto_ships(
        self, repo: Path
    ) -> None:
        """A landed plan corroborates a shipped_in SHA the same way an
        implemented plan does -- it just isn't sufficient evidence alone
        (see the no-shipped_in test above)."""
        sha = _commit_file(
            repo,
            "landed_backed_module/core.py",
            "print('landed backed module')\n",
            "memo: seed landed backed module (mechanical)",
        )
        _write_plan(repo, "docs/plans/2026-01-01-landed-backed.md", "landed")

        handoff = {
            "id": "h-landed-backed",
            "scope": [
                "landed_backed_module/core.py",
                "docs/plans/2026-01-01-landed-backed.md",
            ],
            "title": "Landed Backed Module",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha
        assert any("status:landed" in e for e in result["evidence"])


class TestCrossHandoffScopeOverlap:
    def test_shared_file_level_scope_overlap_demotes_both_to_surface(self, repo: Path) -> None:
        # File-level (non-directory) scope overlap, post-Defect-2b -- the shared
        # pathspec is a leaf file both handoffs name explicitly, so the attribution
        # count still credits the overlap.
        sha = _commit_file(
            repo,
            "shared_module/shared_thing.py",
            "print('shared thing')\n",
            "feat: land shared thing widget",
        )

        handoff_a = _handoff(
            "h-a", ["shared_module/shared_thing.py"], "Shared Thing Widget A"
        )
        handoff_b = _handoff(
            "h-b", ["shared_module/shared_thing.py"], "Shared Thing Widget B"
        )

        result_a = evaluate_commit_reality(
            handoff_a, repo, _DEFAULT_POLICY, [handoff_b]
        )
        result_b = evaluate_commit_reality(
            handoff_b, repo, _DEFAULT_POLICY, [handoff_a]
        )

        assert result_a["verdict"] == "surface"
        assert result_b["verdict"] == "surface"
        assert result_a["candidate_sha"] == sha
        assert result_b["candidate_sha"] == sha


class TestDirectoryScopeOverlapDoesNotCountTowardAttribution:
    """2026-07-20 claude-central-em false-positive memo, Defect 2b: an "other"
    open handoff whose scope is a bare directory pathspec must NOT count toward
    the cross-handoff attribution guard -- only file-level overlap discriminates
    one handoff's scope from another's. Regression guard for the false-positive
    pattern where a dozen open stubs sharing `coordinator_core/` verbatim
    inflated the ambiguous-attribution count on every commit touching that tree."""

    def test_other_handoffs_directory_only_scope_does_not_demote(self, repo: Path) -> None:
        sha = _commit_file(
            repo,
            "coordinator_core/ops/widget_launcher.py",
            "print('widget launcher')\n",
            "feat: land widget launcher module",
        )

        handoff = _handoff(
            "h-widget-launcher",
            ["coordinator_core/ops/widget_launcher.py"],
            "Widget Launcher Module",
        )
        other = _handoff(
            "h-other-dir-only", ["coordinator_core/ops/"], "Some Other Ops Stub"
        )

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [other])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha


class TestDirectoryScopeContributesNoTokens:
    """2026-07-20 claude-central-em false-positive memo, Defect 2a: a bare-directory
    scope entry contributes zero subject-match tokens -- only a leaf FILE
    basename's stem is descriptive vocabulary."""

    def test_directory_scope_entry_contributes_no_tokens(self) -> None:
        assert _derive_noun_tokens(["coordinator_core/ops/"], "") == set()

    def test_file_scope_entry_still_contributes_stem_tokens(self) -> None:
        tokens = _derive_noun_tokens(["coordinator_core/ops/widget_launcher.py"], "")
        assert {"widget", "launcher"} <= tokens
        # "ops"/"core" are structural stopwords by default even though this
        # scope entry is a leaf file -- but they never entered the token set in
        # the first place because they're path COMPONENTS, not the stem.
        assert "ops" not in tokens
        assert "core" not in tokens


class TestSubjectMatchMinTokensThreshold:
    """2026-07-20 claude-central-em false-positive memo, Defect 2a: signal (a)
    now requires >=2 distinct matched tokens by default (was: any single token)."""

    def test_single_token_match_is_insufficient_by_default(self) -> None:
        assert _subject_matches_tokens("fix: register ops fixture", {"ops", "widget"}) is False

    def test_two_distinct_token_match_meets_default_threshold(self) -> None:
        assert (
            _subject_matches_tokens("fix: land widget launcher", {"widget", "launcher"})
            is True
        )

    def test_min_tokens_is_policy_overridable(self) -> None:
        assert (
            _subject_matches_tokens(
                "fix: register widget fixture", {"widget", "launcher"}, min_tokens=1
            )
            is True
        )


class TestDeliverableRequiresFile:
    """2026-07-20 claude-central-em false-positive memo, Defect 2 signal (b): an
    existing DIRECTORY no longer counts as "deliverable present" under the
    default `deliverable_requires_file=True` -- only a leaf file (or a glob
    hitting >=1 file) does."""

    def test_directory_only_scope_is_not_present_by_default(self, tmp_path: Path) -> None:
        (tmp_path / "some_dir").mkdir()

        assert _deliverable_present(tmp_path, ["some_dir/"]) is False

    def test_directory_only_scope_is_present_with_require_file_false(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "some_dir").mkdir()

        assert _deliverable_present(tmp_path, ["some_dir/"], require_file=False) is True

    def test_leaf_file_scope_is_present_by_default(self, tmp_path: Path) -> None:
        (tmp_path / "some_file.py").write_text("x\n", encoding="utf-8")

        assert _deliverable_present(tmp_path, ["some_file.py"]) is True


class TestSelfReferentialScopeIsNotADeliverable:
    """2026-07-20 claude-central-em EM follow-up: a handoff whose scope is only
    its OWN handoff doc + the tracker file is not evidence of anything. Prior to
    this fix, such a handoff could satisfy signal (a) (title tokens matching an
    unrelated tracker-touching commit) and signal (b) (its own doc + tracker both
    trivially exist), producing a spurious `auto-ship`. Regression guard for the
    exact live false positive: `f3a5324e...` scoped to
    `[state/handoffs/<own>.md, state/handoff-tracker.md]`, `deployment_state:
    awaiting_gate`, attributed to an unrelated tracker-touching commit."""

    def test_self_referential_scope_does_not_auto_ship(self, repo: Path) -> None:
        _commit_file(
            repo,
            "state/handoff-tracker.md",
            "tracker\n",
            "handoff(claude-klabauter-generation-leg): execution handoff — plan execution-authorized",
        )
        own_doc = "state/handoffs/2026-07-04_125734_f3a5324e.md"
        (repo / own_doc).parent.mkdir(parents=True, exist_ok=True)
        (repo / own_doc).write_text("---\n---\n", encoding="utf-8")

        handoff = {
            "id": "2026-07-04_125734_f3a5324e",
            "scope": [own_doc, "state/handoff-tracker.md"],
            "title": "claude-klabauter-side doctrine-excision — repatriate mis-homed files",
            "created": "2020-01-01",
            "deployment_state": "awaiting_gate",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] != "auto-ship"

    def test_self_referential_scope_contributes_no_tokens(self) -> None:
        tokens = _derive_noun_tokens(
            ["state/handoffs/2026-07-04_125734_f3a5324e.md", "state/handoff-tracker.md"],
            "claude-klabauter-side doctrine-excision",
        )
        # "claude-klabauter" is a stopword; the two scope entries are non-deliverable, so
        # only "excision" (and "doctrine", "side") survive from the title.
        assert "tracker" not in tokens
        assert "f3a5324e" not in tokens


class TestAwaitingGateNeverAutoShips:
    """2026-07-20 EM follow-up (3): an `awaiting_gate` handoff must never emit
    `auto-ship` from commit_reality, regardless of how strong the three-signal
    evidence is — `gate_eval` is the sole clearing authority, and the caller in
    `handoff_reconcile.py` checks commit_reality's verdict BEFORE its own
    awaiting_gate branch, so an ungated auto-ship here would bypass the gate."""

    def test_awaiting_gate_demotes_clear_ship_to_surface(self, repo: Path) -> None:
        sha = _commit_file(
            repo,
            "widget_engine/core.py",
            "print('widget engine')\n",
            "feat: land widget engine core",
        )
        handoff = _handoff("h-widget-gated", ["widget_engine/core.py"], "Widget Engine")
        handoff["deployment_state"] = "awaiting_gate"

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "surface"
        assert result["candidate_sha"] == sha
        assert any("awaiting_gate" in e for e in result["evidence"])


class TestDiscriminatingPathspecs:
    def test_filters_directory_entries_keeps_file_entries(self) -> None:
        result = _discriminating_pathspecs(
            ["coordinator_core/ops/", "coordinator_core/ops/x.py", ""]
        )

        assert result == ["coordinator_core/ops/x.py"]


class TestPathspecOverlapsSuffixBoundary:
    """Code-review Finding 1 (P1): the bare string-prefix branch of
    `_pathspec_overlaps` (no "/" boundary check) previously reported a touched
    file as "overlapping" a scope pathspec that is merely a filename-suffix
    extension of it (e.g. `module.py.bak` vs touched `module.py`) — a false
    "scope subsumed" verdict with no shared directory containment. Proves the
    fix and guards the regression."""

    def test_filename_suffix_extension_is_not_an_overlap(self) -> None:
        touched = {"src/module.py"}
        assert _pathspec_overlaps(["src/module.py.bak"], touched) is False
        assert _pathspec_overlaps(["src/module.py.orig"], touched) is False
        assert _pathspec_overlaps(["src/module.pyc"], touched) is False

    def test_directory_containment_still_overlaps(self) -> None:
        # Sanity: the intended directory-boundary semantics still hold both
        # directions after the fix.
        touched = {"src/module"}
        assert _pathspec_overlaps(["src/module/core.py"], touched) is True


class TestCrossRepoScopeEntryRecognition:
    """Cross-repo `scope:` entries (`<repo-id>:<path>`, e.g.
    `claude-klabauter:coordinator_core/dag.py`) were previously silently
    inert -- they never prefix-matched a touched path, never resolved on
    disk, so a baton scoped cross-repo produced no evidence and no
    explanation. `_is_cross_repo_scope_entry` / `_split_cross_repo_scope`
    recognize the documented grammar (mirrors
    `coordinator_core/pickup_assemble/__init__.py::_SCOPE_SIBLING_PREFIX_RE`)
    so the matcher can name what it skipped instead of silently dropping it.

    2026-07-27 fix: `_SCOPE_SIBLING_PREFIX_RE` previously required
    MANDATORY whitespace after the colon (`\\s+`), a form no real plan
    ever writes (YAML parses `- repo: path`, with a space, as a mapping,
    not the plain string a scope list wants -- every real author writes
    `- repo:path`, no space). That made the "documented" grammar one no
    author could structurally produce, so this whole code path was dead on
    arrival. Whitespace is now OPTIONAL (`\\s*`); both forms below are
    covered."""

    def test_recognizes_documented_cross_repo_form_no_space(self) -> None:
        # The form every real plan/handoff scope entry actually uses (no
        # space after the colon -- see class docstring for why).
        assert _is_cross_repo_scope_entry("claude-klabauter:coordinator_core/dag.py") is True
        assert _is_cross_repo_scope_entry("example-retrieval-repo:chunker/embed.py") is True

    def test_recognizes_documented_cross_repo_form_with_space(self) -> None:
        # Still accepted -- whitespace after the colon is optional, not
        # forbidden.
        assert _is_cross_repo_scope_entry("claude-klabauter: coordinator_core/dag.py") is True
        assert _is_cross_repo_scope_entry("example-retrieval-repo: chunker/embed.py") is True

    def test_does_not_misfire_on_windows_drive_letter_path(self) -> None:
        # A drive letter is exactly ONE character before its colon; the
        # repo-id group requires TWO OR MORE (`[A-Za-z][A-Za-z0-9_-]+`, a
        # trailing `+` not `*`). This is what disambiguates a drive letter
        # from a repo-id prefix -- it holds regardless of whitespace after
        # the colon, so it is unaffected by the now-optional `\s*`.
        assert _is_cross_repo_scope_entry(r"C:\Users\test\file.py") is False
        assert _is_cross_repo_scope_entry(r"C:\Users\foo\bar") is False
        assert _is_cross_repo_scope_entry("D:/foo/bar") is False

    def test_does_not_misfire_on_url(self) -> None:
        # `https` is 5 characters -- it satisfies the repo-id shape, and
        # with whitespace now optional a URL's `://` would otherwise read
        # as a valid (zero-whitespace) colon separator. The `(?!//)`
        # negative lookahead in `_SCOPE_SIBLING_PREFIX_RE` excludes any
        # `scheme://...` shape outright: no real `<repo-id>:<path>` scope
        # entry's path half starts with `//`.
        assert _is_cross_repo_scope_entry("https://example.com/x") is False
        assert _is_cross_repo_scope_entry("http://example.com/x") is False

    def test_does_not_misfire_on_ordinary_local_path(self) -> None:
        assert _is_cross_repo_scope_entry("coordinator_core/ops/x.py") is False
        assert _is_cross_repo_scope_entry("docs/plans/2026-07-01-widget.md") is False

    def test_does_not_misfire_on_bare_path_or_prose_with_colon(self) -> None:
        # A bare path with no colon at all, and a prose line that happens
        # to contain a colon but isn't repo-id-shaped (starts with a
        # non-alpha token before the colon).
        assert _is_cross_repo_scope_entry("coordinator_core/reconcile/commit_reality.py") is False
        assert _is_cross_repo_scope_entry("see docs/plans/x.md: the linked plan") is False

    def test_split_partitions_and_preserves_order(self) -> None:
        scope = [
            "coordinator_core/ops/x.py",
            "claude-klabauter:coordinator_core/dag.py",
            "docs/plans/2026-07-01-widget.md",
            "example-retrieval-repo: chunker/embed.py",
            "",
        ]
        local, cross_repo = _split_cross_repo_scope(scope)
        assert local == ["coordinator_core/ops/x.py", "docs/plans/2026-07-01-widget.md"]
        assert cross_repo == [
            "claude-klabauter:coordinator_core/dag.py",
            "example-retrieval-repo: chunker/embed.py",
        ]


class TestExplicitShipClaimCrossRepoScopeVisibility:
    """A baton scoped cross-repo is no longer invisible to the resolver --
    `_evaluate_explicit_ship_claim` now names the skipped entries in its
    evidence trail instead of letting them silently fail to match. Both
    tests below seed their evidence commit with a `memo:`-prefixed subject,
    so `_is_mechanical_subject`'s denylist short-circuit (checked, and
    short-circuited on, before token-overlap in `_find_candidate_commits`,
    `commit_reality.py:366-369`) excludes that commit from signal (a)'s
    candidate list regardless of how much vocabulary it shares with the
    handoff's title/scope -- that denylist short-circuit, not vocabulary
    disjointness, is what forces these tests through
    `_evaluate_explicit_ship_claim` rather than the ordinary signal-(a)
    subject-match path (code-review finding, 2026-07-27: the first test
    below actually shares two tokens, "brontosaur"/"widget", with its seed
    commit subject -- it is still safe, for the reason just given, not
    because the vocabulary happens to be disjoint). Each test independently
    proves the intended path ran via the
    `_EXPLICIT_SHIP_CLAIM_SCOPE_OVERLAP_MARKER` assertion below."""

    def test_mixed_local_and_cross_repo_scope_ordinary_evidence_still_works(
        self, repo: Path
    ) -> None:
        sha = _commit_file(
            repo,
            "brontosaur_widget/core.py",
            "print('brontosaur widget')\n",
            "memo: seed brontosaur widget (mechanical, never token-matched)",
        )
        handoff = {
            "id": "h-brontosaur",
            "scope": [
                # No-space form -- the shape every real plan/handoff scope
                # entry actually uses (see class docstring above).
                "claude-klabauter:coordinator_core/unrelated_sibling_dag.py",
                "brontosaur_widget/core.py",
            ],
            "title": "Brontosaur Widget Rollout",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        assert result["verdict"] == "auto-ship"
        assert result["candidate_sha"] == sha
        assert result["confidence"] == "high"
        assert any(
            _EXPLICIT_SHIP_CLAIM_SCOPE_OVERLAP_MARKER in e for e in result["evidence"]
        )
        assert any(
            "scope entries name another repo" in e
            and "coordinator_core/unrelated_sibling_dag.py" in e
            for e in result["evidence"]
        )

    def test_entirely_cross_repo_scope_verdict_unchanged_but_evidence_explains(
        self, repo: Path
    ) -> None:
        sha = _commit_file(
            repo,
            "unrelated_local_file.py",
            "print('unrelated local file')\n",
            "memo: seed unrelated local file (mechanical, never token-matched)",
        )
        handoff = {
            "id": "h-ferret",
            "scope": [
                # No-space form -- the shape every real plan/handoff scope
                # entry actually uses (see class docstring above).
                "claude-klabauter:coordinator_core/ferret_tunnel_dag.py",
                "example-retrieval-repo:chunker/ferret_tunnel_embed.py",
            ],
            "title": "Ferret Tunnel Migration",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        # Unchanged from today's behaviour: no local deliverable can ever be
        # present when every scope entry names a sibling repo, so this
        # degrades to `surface`, never a silent no-match and never a
        # promoted auto-ship.
        assert result["verdict"] == "surface"
        assert any("deliverable absent on disk" in e for e in result["evidence"])
        assert any(
            "scope entries name another repo" in e
            and "coordinator_core/ferret_tunnel_dag.py" in e
            and "chunker/ferret_tunnel_embed.py" in e
            for e in result["evidence"]
        )


class TestMalformedScopePrefixEntryEvidence:
    """Code-review F1: a scope entry that merely LOOKS like the documented
    `<repo-id>: <path>` cross-repo form but whose `<rest>` half is prose,
    not a path -- e.g. `"example-retrieval-repo: doctrine-corpus indexing (PHASE 2 --
    gated on de-bashing)"` -- is not a genuine cross-repo entry.
    `pickup_assemble.compute_tree_quiescence` (the grammar's SSOT) rejects
    this exact shape into `unparseable_scope_entries` (its own discriminator:
    the captured `rest` is empty or contains whitespace); this matcher's
    evidence trail must say the same thing, not report it as a legitimately
    -skipped sibling-repo path. Verdict-neutral by construction: a malformed
    entry never resolved as a local pathspec either way, before or after
    this fix -- only the evidence wording changes."""

    def test_malformed_prefixed_entry_reports_as_malformed_not_genuine_cross_repo(
        self, repo: Path
    ) -> None:
        sha = _commit_file(
            repo,
            "kelpie_module/core.py",
            "print('kelpie module')\n",
            "memo: seed kelpie module (mechanical, never token-matched)",
        )
        handoff = {
            "id": "h-kelpie",
            "scope": [
                "kelpie_module/core.py",
                "example-retrieval-repo: doctrine-corpus indexing (PHASE 2 — gated on de-bashing)",
            ],
            "title": "Kelpie Module Rollout",
            "created": "2020-01-01",
            "shipped_in": sha,
            "shipped_in_kind": "ship-commit",
        }

        result = evaluate_commit_reality(handoff, repo, _DEFAULT_POLICY, [])

        # Verdict-neutral: the malformed entry is excluded from local scope
        # either way, so the genuine local scope entry still clears the top
        # tier exactly as it would with the malformed entry absent entirely.
        assert result["verdict"] == "auto-ship"
        assert result["confidence"] == "high"
        assert any(
            "malformed" in e and "doctrine-corpus indexing" in e
            for e in result["evidence"]
        )
        assert not any(
            "name another repo" in e and "doctrine-corpus indexing" in e
            for e in result["evidence"]
        )


class TestScopeSiblingPrefixGrammarStaysInSync:
    """`_SCOPE_SIBLING_PREFIX_RE` is deliberately duplicated VERBATIM across
    three call sites -- the grammar's single source of truth for the
    cross-repo `<repo-id>: <path>` `scope:` form:

      1. coordinator_core/pickup_assemble/__init__.py    (canonical copy)
      2. coordinator_core/reconcile/commit_reality.py
      3. coordinator_core/execute_plan_assemble/close_out_and_stamp.py

    Adding a FOURTH copy anywhere else in the package? Register its module
    path in `_KNOWN_COPY_MODULES` below, or `test_no_undeclared_copies_exist`
    (the discovery half of this guard) will fail and tell you to.

    The duplication is deliberate, not an oversight to clean up: importing
    `pickup_assemble` (a very large module) at `commit_reality` module scope
    would tax a resolver that has to hold a sub-10ms/zero-spawn budget. A
    hand-maintained comment saying "these must match" does not stop drift --
    nothing re-checks it when any side is edited. `close_out_and_stamp.py`'s
    copy briefly drifted on 2026-07-27 (one pattern used `:` followed by
    optional whitespace then a negative lookahead for `/`, the other two's
    used `:` with a negative lookahead for `//` then optional whitespace --
    both reject URLs, but they disagree on whether
    `repo-id:/abs/path` is a sibling reference) while this test only covered
    the first two copies -- caught by eye, not by this guard. This class is
    the gate that gives the "must match" comments teeth: it fails loudly the
    moment ANY pair of copies diverges -- either in source-string text OR in
    compile flags (e.g. one side gaining `re.IGNORECASE` while the source
    string stays identical would otherwise slip past a `.pattern`-only
    check) -- naming exactly which copy diverged from which, so the fix is
    "update the drifted one to match", never "delete this test" or "import
    pickup_assemble at module scope after all".

    Function-scoped imports throughout (not module-scope) for the same
    reason `commit_reality.py` itself avoids a module-scope
    `pickup_assemble` import -- see that module's own comment above its
    `_SCOPE_SIBLING_PREFIX_RE` declaration."""

    _KNOWN_COPY_MODULES: tuple[str, ...] = (
        "coordinator_core.pickup_assemble",
        "coordinator_core.reconcile.commit_reality",
        "coordinator_core.execute_plan_assemble.close_out_and_stamp",
    )

    def _load_known_copies(self) -> dict:
        """Function-scoped import of every known-copy module -- returns
        {module_path: compiled re.Pattern}. Kept as a shared helper so both
        tests below import exactly once per module, not once per assertion."""
        import importlib

        return {
            mod_name: getattr(
                importlib.import_module(mod_name), "_SCOPE_SIBLING_PREFIX_RE"
            )
            for mod_name in self._KNOWN_COPY_MODULES
        }

    def test_pattern_source_strings_and_flags_are_identical(self) -> None:
        patterns = self._load_known_copies()
        ref_name = self._KNOWN_COPY_MODULES[0]
        ref_pattern = patterns[ref_name]

        for mod_name in self._KNOWN_COPY_MODULES[1:]:
            candidate = patterns[mod_name]
            assert candidate.pattern == ref_pattern.pattern, (
                f"{mod_name}._SCOPE_SIBLING_PREFIX_RE has drifted from "
                f"{ref_name}._SCOPE_SIBLING_PREFIX_RE.\n"
                f"  {mod_name}: {candidate.pattern!r}\n"
                f"  {ref_name}: {ref_pattern.pattern!r}\n"
                "These copies are DELIBERATELY duplicated across three call "
                "sites (see the class docstring for why) -- "
                f"{ref_name}'s copy is the grammar's single source of "
                f"truth. Update {mod_name}'s copy to match it verbatim; do "
                "not weaken or delete this assertion, and do not convert "
                "any copy into an import."
            )
            assert candidate.flags == ref_pattern.flags, (
                f"{mod_name}._SCOPE_SIBLING_PREFIX_RE compile flags have "
                f"drifted from {ref_name}._SCOPE_SIBLING_PREFIX_RE.\n"
                f"  {mod_name}: flags={candidate.flags!r} pattern={candidate.pattern!r}\n"
                f"  {ref_name}: flags={ref_pattern.flags!r} pattern={ref_pattern.pattern!r}\n"
                "Identical source-string text is not enough if one side "
                "gains a flag like re.IGNORECASE the other lacks. Update "
                f"{mod_name}'s copy to match verbatim, same as the "
                "pattern-string assertion above."
            )

    def test_no_undeclared_copies_exist(self) -> None:
        """Discovery half of the guard: greps the coordinator_core package
        tree for `_SCOPE_SIBLING_PREFIX_RE = re.compile(...)` DECLARATION
        lines (not references/imports of the name) and asserts the set of
        files declaring a copy is exactly `_KNOWN_COPY_MODULES` -- no more,
        no fewer. This is what makes a FOURTH copy visible to the guard
        even if whoever added it forgot to update this test file: the
        assertion below fails and names the undiscovered file, rather than
        silently passing because `test_pattern_source_strings_and_flags_are_identical`
        only ever looks at the three modules it already knows about."""
        import re as re_module
        from pathlib import Path

        package_root = Path(__file__).resolve().parents[2]  # .../coordinator_core
        assert package_root.name == "coordinator_core", (
            f"expected to resolve to coordinator_core, got {package_root}"
        )

        declaration_re = re_module.compile(
            r"^_SCOPE_SIBLING_PREFIX_RE\s*=\s*re\.compile\("
        )

        discovered: set[str] = set()
        for py_file in package_root.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            try:
                text = py_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line in text.splitlines():
                if declaration_re.match(line.strip()):
                    rel = py_file.relative_to(package_root.parent).as_posix()
                    discovered.add(rel)
                    break

        expected = {
            mod_name.replace(".", "/") + (
                "/__init__.py" if mod_name == "coordinator_core.pickup_assemble" else ".py"
            )
            for mod_name in self._KNOWN_COPY_MODULES
        }

        extra = discovered - expected
        missing = expected - discovered
        assert not extra and not missing, (
            "_SCOPE_SIBLING_PREFIX_RE declaration sites on disk don't match "
            "this guard's _KNOWN_COPY_MODULES list.\n"
            f"  declared on disk but NOT registered here: {sorted(extra) or 'none'}\n"
            f"  registered here but NOT found on disk:    {sorted(missing) or 'none'}\n"
            "If a new copy was added, register its module path in "
            "TestScopeSiblingPrefixGrammarStaysInSync._KNOWN_COPY_MODULES "
            "(coordinator_core/reconcile/tests/test_commit_reality.py) so "
            "test_pattern_source_strings_and_flags_are_identical also "
            "checks it. If a copy was removed, drop it from that list."
        )
