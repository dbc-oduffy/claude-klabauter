
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import core, grant
from coordinator_core.win_portability import no_console_passthrough_kwargs

# ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=tmp_path,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs()
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs()
    )
    return tmp_path


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
    return Path(repo) / ".git" / "coordinator-sessions" / sid / "tier-u-grant.json"


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
        ok = grant.write_tier_u_grant(
            "pm", "ask", session_id="s1", cwd=str(tmp_path / "not-a-repo")
        )
        assert ok is False


class TestCheckTierUGrantLiveness:

    def test_live_session_with_valid_grant_is_granted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask", session_id="s1", cwd=str(repo))
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is True
        assert record["session_id"] == "s1"

    def test_dead_session_grant_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "ask before crash", session_id="s1", cwd=str(repo))
        _dead_session(repo, "s1")
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None
        assert record["note"] == "ask before crash"

    def test_meta_less_session_falls_back_to_dir_mtime_and_is_treated_live(self, tmp_path):
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
        old_epoch = 946684800
        os.utime(gfile, (old_epoch, old_epoch))
        granted, _record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is False


class TestCheckTierUGrantNoGlob:

    def test_sibling_live_grant_does_not_authorize_caller(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-sibling")
        _live_session(repo, "s-caller")
        grant.write_tier_u_grant(
            "pm", "sibling's own ask", session_id="s-sibling", cwd=str(repo)
        )
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
        assert record is not None

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


class TestRevokeTierUGrant:

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


class TestGuardedCeremonyHandback:

    def test_handback_removes_own_ceremony_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant(
            "ceremony", "note", ceremony="workweek-complete", session_id="s1", cwd=str(repo)
        )
        ok = grant.revoke_tier_u_grant(
            cwd=str(repo), session_id="s1", only_ceremony="workweek-complete"
        )
        assert ok is True
        assert not _grant_file(repo, "s1").exists()

    def test_handback_never_destroys_a_pm_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "the PM said run it", session_id="s1", cwd=str(repo))

        ok = grant.revoke_tier_u_grant(
            cwd=str(repo), session_id="s1", only_ceremony="workweek-complete"
        )
        assert ok is True, "a guarded no-op is success, not a failed directive"

        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is True
        assert record["note"] == "the PM said run it"

    def test_handback_leaves_another_ceremonys_grant_alone(self, tmp_path):
        """The `/workweek-complete` -> `/merging-to-main` nesting case: one
        grant file per session means the nested write REPLACED workweek's
        record, so workweek's outer handback must find merge's grant and
        no-op rather than revoking it."""
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant(
            "ceremony", "note", ceremony="merging-to-main", session_id="s1", cwd=str(repo)
        )

        outer = grant.revoke_tier_u_grant(
            cwd=str(repo), session_id="s1", only_ceremony="workweek-complete"
        )
        assert outer is True
        granted, record = grant.check_tier_u_grant(cwd=str(repo), session_id="s1")
        assert granted is True
        assert record["ceremony"] == "merging-to-main"

        inner = grant.revoke_tier_u_grant(
            cwd=str(repo), session_id="s1", only_ceremony="merging-to-main"
        )
        assert inner is True
        assert not _grant_file(repo, "s1").exists()

    def test_handback_absent_grant_is_idempotent_success(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        ok = grant.revoke_tier_u_grant(
            cwd=str(repo), session_id="s1", only_ceremony="workweek-complete"
        )
        assert ok is True

    def test_handback_leaves_an_unattributable_record_alone(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant_file = _grant_file(repo, "s1")
        grant_file.parent.mkdir(parents=True, exist_ok=True)
        grant_file.write_text("{not json", encoding="utf-8")

        ok = grant.revoke_tier_u_grant(
            cwd=str(repo), session_id="s1", only_ceremony="workweek-complete"
        )
        assert ok is True
        assert grant_file.exists()

    def test_unguarded_revoke_shape_is_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant(
            "ceremony", "note", ceremony="merging-to-main", session_id="s1", cwd=str(repo)
        )
        ok = grant.revoke_tier_u_grant(cwd=str(repo), session_id="s1")
        assert ok is True
        assert not _grant_file(repo, "s1").exists()


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
        record = grant.read_tier_u_grant(cwd=str(repo), session_id="s1")
        assert record is not None
        assert record["note"] == "ask"
