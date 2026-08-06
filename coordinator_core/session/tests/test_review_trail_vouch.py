"""
coordinator_core.session.tests.test_review_trail_vouch — tests for
coordinator_core.session.review_trail_vouch, the per-session, PM-granted
relaxation of the review-trail write-side foreign-session Case-1 refusal.

Fixtures mirror ``test_grant.py`` (the sibling Tier-U grant test this module
is deliberately shaped after) — real ``tmp_path`` git repos, liveness driven
via ``meta.json``'s ``last_activity`` field.

Spec backlink: coordinator_core/session/review_trail_vouch.py
Spec backlink: archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C7
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, review_trail_vouch as vouch


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _commit(repo, name):
    """Create a real commit (distinct content per ``name``) and return its
    full 40-char SHA — Finding 3 requires ``write_review_trail_vouch`` to
    resolve every sha token via ``git rev-parse``, so a fake hex string like
    the pre-Finding-3 fixtures used is no longer a valid grant token."""
    (Path(repo) / f"{name}.txt").write_text(name)
    subprocess.run(["git", "add", "."], cwd=repo)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip()


def _write_session_meta(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _live_session(repo, sid):
    return _write_session_meta(repo, sid, {"pid": "999", "last_activity": core.now_iso()})


def _dead_session(repo, sid):
    return _write_session_meta(
        repo, sid, {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"}
    )


def _grant_file(repo, sid):
    return Path(repo) / ".git" / "coordinator-sessions" / sid / "review-trail-vouch.json"


# ---------------------------------------------------------------------------
# write_review_trail_vouch — round-trip, verbatim note, validation
# ---------------------------------------------------------------------------


class TestWriteReviewTrailVouch:
    def test_pm_grant_round_trips(self, tmp_path):
        """Finding 3: a PM utterance naming an ABBREVIATED sha (the human
        workflow this mechanism exists to unblock — DR-243's own motivating
        example, `c12623534`, is 9 chars) resolves to, and is stored as, the
        FULL 40-char commit SHA."""
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        full_sha = _commit(repo, "peer-commit")
        abbreviated = full_sha[:9]
        ok = vouch.write_review_trail_vouch(
            [abbreviated], "reviewed live, found two P1 security bypasses",
            session_id="s1", cwd=str(repo),
        )
        assert ok is True
        record = vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1")
        assert record["granted_by"] == "pm"
        assert record["session_id"] == "s1"
        assert record["shas"] == [full_sha]
        assert record["note"] == "reviewed live, found two P1 security bypasses"
        assert "granted_at" in record

    def test_shas_deduplicated_and_sorted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        sha_b = _commit(repo, "commit-b")
        vouch.write_review_trail_vouch(
            [sha_b, sha_a, sha_b], "two distinct shas",
            session_id="s1", cwd=str(repo),
        )
        record = vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1")
        assert record["shas"] == sorted([sha_a, sha_b])

    def test_overwrite_replaces_prior_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        sha_b = _commit(repo, "commit-b")
        vouch.write_review_trail_vouch([sha_a], "first ask", session_id="s1", cwd=str(repo))
        vouch.write_review_trail_vouch([sha_b], "second ask", session_id="s1", cwd=str(repo))
        record = vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1")
        assert record["note"] == "second ask"
        assert record["shas"] == [sha_b]

    def test_atomic_write_no_temp_file_left_behind(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch([sha_a], "ask", session_id="s1", cwd=str(repo))
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "s1"
        leftovers = [p for p in sdir.iterdir() if p.name.startswith("review-trail-vouch.json.")]
        assert leftovers == []
        assert _grant_file(repo, "s1").is_file()


class TestWriteReviewTrailVouchValidation:
    def test_unknown_granted_by_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            vouch.write_review_trail_vouch(
                ["aaa1111"], "ask", granted_by="robot", session_id="s1", cwd=str(repo)
            )

    def test_empty_note_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            vouch.write_review_trail_vouch(["aaa1111"], "", session_id="s1", cwd=str(repo))

    def test_empty_shas_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            vouch.write_review_trail_vouch([], "ask", session_id="s1", cwd=str(repo))

    def test_blank_shas_only_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            vouch.write_review_trail_vouch(["", "  "], "ask", session_id="s1", cwd=str(repo))

    def test_unresolvable_session_returns_false_not_raise(self, tmp_path):
        ok = vouch.write_review_trail_vouch(
            ["aaa1111"], "ask", session_id="s1", cwd=str(tmp_path / "not-a-repo")
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Finding 3 — sha-token expansion at grant time (git rev-parse <token>^{commit})
# ---------------------------------------------------------------------------


class TestWriteReviewTrailVouchShaExpansion:
    """A token that git cannot resolve to exactly one commit — unknown, or
    ambiguous — must fail LOUD at grant time (never stored as-is, never
    silently authorizing nothing at check time)."""

    def test_unknown_token_fails_loud(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError, match="did not resolve to a unique commit"):
            vouch.write_review_trail_vouch(
                ["deadbeef"], "ask", session_id="s1", cwd=str(repo)
            )
        # Never stored as-is.
        assert vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1") is None

    def test_ambiguous_prefix_fails_loud_at_expand_sha(self, monkeypatch):
        """A real ambiguous-SHA collision cannot be reliably mined on demand
        (content-addressed, environment-dependent) — mirrors the mocking
        idiom `test_batch_check_hex_tokens_ambiguous_token_not_treated_as_
        resolved` (coordinator_core/tests/test_coverage_reviewed_set.py)
        uses for the same reason: replay git's actual ambiguous-prefix
        failure shape (non-zero exit, `--verify --quiet` does not suppress
        the ambiguity error) via a stubbed subprocess.run, scoped to
        `_expand_sha` directly rather than the full write_review_trail_vouch
        path so this doesn't also stub out the session-dir git resolution
        `write_review_trail_vouch` performs before reaching sha expansion."""

        class _FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "error: short object ID abc1234 is ambiguous\n"

        monkeypatch.setattr(vouch.subprocess, "run", lambda *a, **k: _FakeCompleted())

        assert vouch._expand_sha("abc1234", None) is None

    def test_write_review_trail_vouch_raises_when_a_token_is_unresolvable(
        self, tmp_path, monkeypatch
    ):
        """Integration point: whatever `_expand_sha` returns (unknown OR
        ambiguous — both surface as None, see that function's docstring),
        `write_review_trail_vouch` fails loud, never silently stores or
        silently authorizes nothing."""
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        monkeypatch.setattr(vouch, "_expand_sha", lambda token, cwd: None)
        with pytest.raises(ValueError, match="did not resolve to a unique commit"):
            vouch.write_review_trail_vouch(
                ["whatever-token"], "ask", session_id="s1", cwd=str(repo)
            )
        assert vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1") is None

    def test_resolved_token_is_stored_as_full_sha(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        full_sha = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch(
            [full_sha[:8]], "ask", session_id="s1", cwd=str(repo)
        )
        record = vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1")
        assert record["shas"] == [full_sha]
        assert len(record["shas"][0]) == 40


# ---------------------------------------------------------------------------
# check_review_trail_vouch — liveness + narrow-by-SHA semantics
# ---------------------------------------------------------------------------


class TestCheckReviewTrailVouchLiveness:
    """AC4 — a grant left behind by a crashed/dead session must NOT
    authorize the current one, even for a SHA it explicitly named."""

    def test_live_session_with_valid_grant_authorizes_named_sha(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch([sha_a], "ask", session_id="s1", cwd=str(repo))
        vouched, record = vouch.check_review_trail_vouch(
            [sha_a], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset({sha_a})
        assert record["session_id"] == "s1"

    def test_dead_session_grant_authorizes_nothing(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch(
            [sha_a], "ask before crash", session_id="s1", cwd=str(repo)
        )
        _dead_session(repo, "s1")
        vouched, record = vouch.check_review_trail_vouch(
            [sha_a], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset()
        # Record still returned for audit/denial quoting.
        assert record is not None
        assert record["note"] == "ask before crash"

    def test_resumed_different_session_id_not_authorized(self, tmp_path):
        """A grant is scoped to the GRANTING session's own id — a distinct
        session_id (even if the process were somehow 'resumed' under a new
        id) does not inherit the grant."""
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch([sha_a], "ask", session_id="s1", cwd=str(repo))
        _live_session(repo, "s2")
        vouched, record = vouch.check_review_trail_vouch(
            [sha_a], cwd=str(repo), session_id="s2"
        )
        assert vouched == frozenset()
        assert record is None


class TestCheckReviewTrailVouchNarrowBySha:
    """AC3 — a grant naming SHA X does not authorize a range containing
    foreign SHA Y."""

    def test_grant_for_x_does_not_authorize_y(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_x = _commit(repo, "commit-x")
        sha_y = _commit(repo, "commit-y")
        vouch.write_review_trail_vouch([sha_x], "vouch for X only", session_id="s1", cwd=str(repo))
        vouched, _record = vouch.check_review_trail_vouch(
            [sha_x, sha_y], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset({sha_x})
        assert sha_y not in vouched

    def test_no_grant_authorizes_nothing(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        vouched, record = vouch.check_review_trail_vouch(
            ["x1111111"], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset()
        assert record is None


class TestCheckReviewTrailVouchNoGlob:
    """A sibling session's LIVE grant must not authorize the calling
    session — same negative-spec as session.grant."""

    def test_sibling_live_grant_does_not_authorize_caller(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-sibling")
        _live_session(repo, "s-caller")
        sha_a = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch(
            [sha_a], "sibling's own ask", session_id="s-sibling", cwd=str(repo)
        )
        vouched, record = vouch.check_review_trail_vouch(
            [sha_a], cwd=str(repo), session_id="s-caller"
        )
        assert vouched == frozenset()
        assert record is None


class TestCheckReviewTrailVouchFailClosed:
    def test_malformed_json_authorizes_nothing(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text("{not valid json", encoding="utf-8")
        vouched, record = vouch.check_review_trail_vouch(
            ["aaa1111"], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset()
        assert record is None

    def test_unknown_granted_by_authorizes_nothing(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "s1",
                    "granted_by": "robot",
                    "granted_at": core.now_iso(),
                    "note": "ask",
                    "shas": ["aaa1111"],
                }
            ),
            encoding="utf-8",
        )
        vouched, record = vouch.check_review_trail_vouch(
            ["aaa1111"], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset()
        assert record is not None

    def test_session_id_mismatch_authorizes_nothing(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "someone-else",
                    "granted_by": "pm",
                    "granted_at": core.now_iso(),
                    "note": "ask",
                    "shas": ["aaa1111"],
                }
            ),
            encoding="utf-8",
        )
        vouched, record = vouch.check_review_trail_vouch(
            ["aaa1111"], cwd=str(repo), session_id="s1"
        )
        assert vouched == frozenset()
        assert record is not None


# ---------------------------------------------------------------------------
# read_review_trail_vouch — raw reader, no liveness gate
# ---------------------------------------------------------------------------


class TestReadReviewTrailVouch:
    def test_returns_none_when_absent(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        assert vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1") is None

    def test_reads_dead_session_grant_raw_without_liveness_gate(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        sha_a = _commit(repo, "commit-a")
        vouch.write_review_trail_vouch([sha_a], "ask", session_id="s1", cwd=str(repo))
        _dead_session(repo, "s1")
        record = vouch.read_review_trail_vouch(cwd=str(repo), session_id="s1")
        assert record is not None
        assert record["note"] == "ask"
