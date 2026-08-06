"""
coordinator_core.session.tests.test_grant — tests for
coordinator_core.session.grant, the Tier-U full-suite authorization-grant
writer/reader (DR-088 layer 5).

Fixtures build real ``tmp_path`` git repos with monkeypatch isolation,
mirroring the sibling ``test_shape.py`` / ``test_liveness.py`` idiom in this
package (``_make_repo`` / ``_write_session`` helpers, ``cwd=`` threaded
explicitly rather than a chdir). Liveness is driven by writing/omitting
``meta.json``'s ``last_activity`` field, exactly as ``test_liveness.py``
does for its Layer-2 recency cases.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 2
Spec backlink: example-doctrine-repo docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-authority.md § Decision, layer 5
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, grant


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _write_session_meta(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _live_session(repo, sid):
    """A session whose meta.json makes it read LIVE (fresh last_activity,
    no stable_pid -> Layer-2 recency path)."""
    return _write_session_meta(repo, sid, {"pid": "999", "last_activity": core.now_iso()})


def _dead_session(repo, sid):
    """A session whose meta.json makes it read DEAD (last_activity far in
    the past, no stable_pid -> Layer-2 recency path, stale)."""
    return _write_session_meta(
        repo, sid, {"pid": "999", "last_activity": "2000-01-01T00:00:00Z"}
    )


def _grant_file(repo, sid):
    return Path(repo) / ".git" / "coordinator-sessions" / sid / "tier-u-grant.json"


# ---------------------------------------------------------------------------
# write_tier_u_grant — round-trip, verbatim note, atomicity
# ---------------------------------------------------------------------------


class TestWriteTierUGrant:
    def test_pm_grant_round_trips(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        ok = grant.write_tier_u_grant(
            "pm", "yes, run the full suite", session_id="s1", cwd=str(repo)
        )
        assert ok is True
        record = grant.read_tier_u_grant(cwd=str(repo), session_id="s1")
        assert record["granted_by"] == "pm"
        assert record["session_id"] == "s1"
        assert record["ceremony"] is None
        assert record["note"] == "yes, run the full suite"
        assert "granted_at" in record

    def test_ceremony_grant_round_trips(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        ok = grant.write_tier_u_grant(
            "ceremony",
            "implicit grant for cadence gate",
            ceremony="workday-complete",
            session_id="s1",
            cwd=str(repo),
        )
        assert ok is True
        record = grant.read_tier_u_grant(cwd=str(repo), session_id="s1")
        assert record["granted_by"] == "ceremony"
        assert record["ceremony"] == "workday-complete"

    def test_note_preserved_verbatim_including_whitespace_and_punctuation(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        weird_note = "  yes -- run it, PLEASE.\nmultiple\nlines\t and tabs  "
        grant.write_tier_u_grant("pm", weird_note, session_id="s1", cwd=str(repo))
        record = grant.read_tier_u_grant(cwd=str(repo), session_id="s1")
        assert record["note"] == weird_note

    def test_overwrite_replaces_prior_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "first ask", session_id="s1", cwd=str(repo))
        grant.write_tier_u_grant("pm", "second ask", session_id="s1", cwd=str(repo))
        record = grant.read_tier_u_grant(cwd=str(repo), session_id="s1")
        assert record["note"] == "second ask"

    def test_atomic_write_no_temp_file_left_behind(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask", session_id="s1", cwd=str(repo))
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "s1"
        leftovers = [p for p in sdir.iterdir() if p.name.startswith("tier-u-grant.json.")]
        assert leftovers == []
        assert _grant_file(repo, "s1").is_file()


class TestWriteTierUGrantValidation:
    def test_unknown_granted_by_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            grant.write_tier_u_grant("robot", "ask", session_id="s1", cwd=str(repo))

    def test_ceremony_missing_when_granted_by_ceremony_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            grant.write_tier_u_grant("ceremony", "ask", session_id="s1", cwd=str(repo))

    def test_ceremony_present_when_granted_by_pm_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            grant.write_tier_u_grant(
                "pm", "ask", ceremony="workday-complete", session_id="s1", cwd=str(repo)
            )

    def test_empty_note_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            grant.write_tier_u_grant("pm", "", session_id="s1", cwd=str(repo))

    def test_unresolvable_session_returns_false_not_raise(self, tmp_path):
        # Not a git repo at all -> core.session_dir resolves nothing.
        ok = grant.write_tier_u_grant(
            "pm", "ask", session_id="s1", cwd=str(tmp_path / "not-a-repo")
        )
        assert ok is False


# ---------------------------------------------------------------------------
# check_tier_u_grant — the four DR-088 semantics
# ---------------------------------------------------------------------------


class TestCheckTierUGrantLiveness:
    """Semantic 2 — liveness, not presence. The single most important test:
    a grant left behind by a crashed session must NOT authorize anything."""

    def test_live_session_with_valid_grant_is_granted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask", session_id="s1", cwd=str(repo))
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is True
        assert record["session_id"] == "s1"

    def test_dead_session_grant_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Session was live at write time...
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask before crash", session_id="s1", cwd=str(repo))
        # ...then crashed (meta.json now shows stale recency).
        _dead_session(repo, "s1")
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        # Record is still returned for audit/denial quoting.
        assert record is not None
        assert record["note"] == "ask before crash"

    def test_meta_less_session_falls_back_to_dir_mtime_and_is_treated_live(self, tmp_path):
        # No meta.json at all for the granting session (e.g. cs_init hasn't
        # flushed yet), but the grant file exists. liveness.session_live's
        # meta-less fallback (example-doctrine-repo 642195ba / 88929bea, see liveness.py) reads
        # dir mtime as the recency source rather than defaulting to
        # confirmed-dead -- a session mid-init is presumed live, not dead.
        # This is Layer-2 recency behavior this module inherits verbatim,
        # not a grant-specific rule; freshly created dirs therefore read
        # GRANTED here (contrast with test_dead_session_grant_reads_ungranted,
        # where the dir mtime IS stale).
        sdir = Path(repo := _make_repo(tmp_path)) / ".git" / "coordinator-sessions" / "s1"
        sdir.mkdir(parents=True)
        (sdir / "tier-u-grant.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "s1",
                    "granted_by": "pm",
                    "granted_at": core.now_iso(),
                    "ceremony": None,
                    "note": "orphan grant",
                }
            ),
            encoding="utf-8",
        )
        granted, _record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is True

    def test_meta_less_but_stale_dir_mtime_reads_ungranted(self, tmp_path):
        # Same meta-less shape as above, but the grant file's mtime (the
        # ONLY regular file in the dir, so it drives the recency fallback)
        # is backdated past the 30-min liveness boundary -- a genuinely
        # stale meta-less dir must still read DEAD (liveness.py's own
        # docstring: "a genuinely stale meta-less dir still reads DEAD").
        repo = _make_repo(tmp_path)
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "s1"
        sdir.mkdir(parents=True)
        gfile = sdir / "tier-u-grant.json"
        gfile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "s1",
                    "granted_by": "pm",
                    "granted_at": core.now_iso(),
                    "ceremony": None,
                    "note": "orphan grant",
                }
            ),
            encoding="utf-8",
        )
        old_epoch = 946684800  # 2000-01-01T00:00:00Z
        os.utime(gfile, (old_epoch, old_epoch))
        granted, _record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False


class TestCheckTierUGrantNoGlob:
    """Semantic 3 — path-scoped read, NEVER glob: a sibling session's LIVE
    grant must not authorize the calling session."""

    def test_sibling_live_grant_does_not_authorize_caller(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-sibling")
        _live_session(repo, "s-caller")
        # Sibling holds a perfectly valid, live grant.
        grant.write_tier_u_grant(
            "pm", "sibling's own ask", session_id="s-sibling", cwd=str(repo)
        )
        # Caller has NO grant of its own.
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s-caller")
        assert granted is False
        assert record is None

    def test_read_is_scoped_to_named_session_dir_only(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-a")
        _live_session(repo, "s-b")
        grant.write_tier_u_grant("pm", "a's ask", session_id="s-a", cwd=str(repo))
        grant.write_tier_u_grant("pm", "b's ask", session_id="s-b", cwd=str(repo))
        record_a = grant.read_tier_u_grant(cwd=str(repo), session_id="s-a")
        record_b = grant.read_tier_u_grant(cwd=str(repo), session_id="s-b")
        assert record_a["note"] == "a's ask"
        assert record_b["note"] == "b's ask"


class TestCheckTierUGrantFailClosed:
    """Semantic 4 — fail closed on authorization: absent, unreadable,
    malformed, and unknown-enum edges all read UNGRANTED."""

    def test_absent_file_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_malformed_json_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text("{not valid json", encoding="utf-8")
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_non_object_json_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text("[1, 2, 3]", encoding="utf-8")
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_unknown_granted_by_reads_ungranted(self, tmp_path):
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
                    "ceremony": None,
                    "note": "ask",
                }
            ),
            encoding="utf-8",
        )
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None  # still returned for audit quoting

    def test_ceremony_cross_field_violation_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "s1",
                    "granted_by": "ceremony",
                    "granted_at": core.now_iso(),
                    "ceremony": None,
                    "note": "ask",
                }
            ),
            encoding="utf-8",
        )
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None

    def test_session_id_mismatch_reads_ungranted(self, tmp_path):
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
                    "ceremony": None,
                    "note": "ask",
                }
            ),
            encoding="utf-8",
        )
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None

    def test_unreadable_file_reads_ungranted(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask", session_id="s1", cwd=str(repo))

        real_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self.name == "tier-u-grant.json":
                raise OSError("simulated unreadable file")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_unresolvable_session_reads_ungranted(self, tmp_path):
        granted, record = grant.check_tier_u_grant(
            cwd=str(tmp_path / "not-a-repo"), session_id="s1"
        )
        assert granted is False
        assert record is None


# ---------------------------------------------------------------------------
# read_tier_u_grant — raw reader, no liveness gate
# ---------------------------------------------------------------------------


class TestRevokeTierUGrant:
    """revoke_tier_u_grant — round trip, idempotence, sibling isolation."""

    def test_write_check_revoke_check_round_trip(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask", session_id="s1", cwd=str(repo))
        granted_before, _ = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted_before is True

        ok = grant.revoke_tier_u_grant(cwd=str(repo), session_id="s1")
        assert ok is True
        assert not _grant_file(repo, "s1").exists()

        granted_after, record_after = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted_after is False
        assert record_after is None

    def test_revoke_absent_grant_is_idempotent_success(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        assert not _grant_file(repo, "s1").exists()
        ok = grant.revoke_tier_u_grant(cwd=str(repo), session_id="s1")
        assert ok is True

        # Double-revoke is still success.
        ok_again = grant.revoke_tier_u_grant(cwd=str(repo), session_id="s1")
        assert ok_again is True

    def test_revoke_in_one_session_leaves_sibling_grant_intact(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-a")
        _live_session(repo, "s-b")
        grant.write_tier_u_grant("pm", "a's ask", session_id="s-a", cwd=str(repo))
        grant.write_tier_u_grant("pm", "b's ask", session_id="s-b", cwd=str(repo))

        ok = grant.revoke_tier_u_grant(cwd=str(repo), session_id="s-a")
        assert ok is True

        granted_a, _ = grant.check_tier_u_grant(cwd=str(repo), session_id="s-a")
        assert granted_a is False

        granted_b, record_b = grant.check_tier_u_grant(cwd=str(repo), session_id="s-b")
        assert granted_b is True
        assert record_b["note"] == "b's ask"

    def test_revoke_unresolvable_session_returns_false(self, tmp_path):
        ok = grant.revoke_tier_u_grant(
            cwd=str(tmp_path / "not-a-repo"), session_id="s1"
        )
        assert ok is False


class TestReadTierUGrant:
    def test_returns_none_when_absent(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        assert grant.read_tier_u_grant(cwd=str(repo), session_id="s1") is None

    def test_reads_dead_session_grant_raw_without_liveness_gate(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask", session_id="s1", cwd=str(repo))
        _dead_session(repo, "s1")
        # raw reader is NOT liveness-gated -- record is still returned.
        record = grant.read_tier_u_grant(cwd=str(repo), session_id="s1")
        assert record is not None
        assert record["note"] == "ask"
