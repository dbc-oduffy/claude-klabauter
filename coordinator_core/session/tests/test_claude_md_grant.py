"""
coordinator_core.session.tests.test_claude_md_grant — tests for
coordinator_core.session.claude_md_grant, the session-scoped PM
authorization grant for CLAUDE.md-class writes (DoE-claude
docs/plans/2026-07-27-claude-md-altitude-triage.md § C5).

Structurally mirrors ``test_grant.py`` (the Tier-U grant's own suite) —
same ``_make_repo`` / ``_write_session_meta`` / ``_live_session`` /
``_dead_session`` fixture idiom, same liveness/no-glob/fail-closed
semantic groupings. ``TestSubagentResolvability`` is the ADDITIONAL
class this module's own module docstring commits to: proof that a grant
written via the default (env-driven) session resolution is visible to a
caller using that same default resolution path — the exact path a C4
guard evaluation running inside a dispatched subagent's tool-call turn
would use, per this module's "SUBAGENT-RESOLVABILITY" docstring section.

Spec backlink: DoE-claude DoE-claude:pln-claude-md-altitude-triage-earn-31f32e § C5
Precedent: coordinator_core/session/tests/test_grant.py
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claude_md_grant as cmg
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `core.git_root()`, consulted when resolving where grant state lives -- reads
# real git state that no mock stands in for. `tmp_path` is function-scoped
# and tests write grant/session state under reused session ids, so the repo
# fixture stays per-test rather than hoisted to module scope. The spawn
# ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly
# not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs())
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
    return Path(repo) / ".git" / "coordinator-sessions" / sid / "claude-md-write-grant.json"


# ---------------------------------------------------------------------------
# write_claude_md_write_grant — round-trip, verbatim note, atomicity, validation
# ---------------------------------------------------------------------------


class TestWriteClaudeMdWriteGrant:
    def test_pm_grant_round_trips(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        ok = cmg.write_claude_md_write_grant(
            "pm", "yes, edit CLAUDE.md this session", session_id="s1", cwd=str(repo)
        )
        assert ok is True
        record = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert record["granted_by"] == "pm"
        assert record["session_id"] == "s1"
        assert record["note"] == "yes, edit CLAUDE.md this session"
        assert "granted_at" in record

    def test_note_preserved_verbatim_including_whitespace_and_punctuation(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        weird_note = "  yes -- go ahead, PLEASE.\nmultiple\nlines\t and tabs  "
        cmg.write_claude_md_write_grant("pm", weird_note, session_id="s1", cwd=str(repo))
        record = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert record["note"] == weird_note

    def test_overwrite_replaces_prior_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        cmg.write_claude_md_write_grant("pm", "first ask", session_id="s1", cwd=str(repo))
        cmg.write_claude_md_write_grant("pm", "second ask", session_id="s1", cwd=str(repo))
        record = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert record["note"] == "second ask"

    def test_atomic_write_no_temp_file_left_behind(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        cmg.write_claude_md_write_grant("pm", "ask", session_id="s1", cwd=str(repo))
        sdir = Path(repo) / ".git" / "coordinator-sessions" / "s1"
        leftovers = [
            p for p in sdir.iterdir() if p.name.startswith("claude-md-write-grant.json.")
        ]
        assert leftovers == []
        assert _grant_file(repo, "s1").is_file()


class TestWriteClaudeMdWriteGrantValidation:
    def test_unknown_granted_by_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            cmg.write_claude_md_write_grant("ceremony", "ask", session_id="s1", cwd=str(repo))

    def test_empty_note_raises(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        with pytest.raises(ValueError):
            cmg.write_claude_md_write_grant("pm", "", session_id="s1", cwd=str(repo))

    def test_unresolvable_session_returns_false_not_raise(self, tmp_path):
        # Not a git repo at all -> core.session_dir resolves nothing.
        ok = cmg.write_claude_md_write_grant(
            "pm", "ask", session_id="s1", cwd=str(tmp_path / "not-a-repo")
        )
        assert ok is False


# ---------------------------------------------------------------------------
# check_claude_md_write_grant — liveness, no-glob sibling-isolation, fail-closed
# ---------------------------------------------------------------------------


class TestCheckClaudeMdWriteGrantLiveness:
    """A grant left behind by a crashed session must NOT authorize a
    CLAUDE.md write."""

    def test_live_session_with_valid_grant_is_granted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        cmg.write_claude_md_write_grant("pm", "ask", session_id="s1", cwd=str(repo))
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is True
        assert record["session_id"] == "s1"

    def test_dead_session_grant_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        cmg.write_claude_md_write_grant("pm", "ask before crash", session_id="s1", cwd=str(repo))
        _dead_session(repo, "s1")
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None
        assert record["note"] == "ask before crash"


class TestCheckClaudeMdWriteGrantNoGlob:
    """A sibling session's LIVE grant must not authorize the caller — the
    fleet's shared-branch reality, several EM sessions routinely sharing
    one working tree."""

    def test_sibling_live_grant_does_not_authorize_caller(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-sibling")
        _live_session(repo, "s-caller")
        cmg.write_claude_md_write_grant(
            "pm", "sibling's own ask", session_id="s-sibling", cwd=str(repo)
        )
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s-caller")
        assert granted is False
        assert record is None

    def test_read_is_scoped_to_named_session_dir_only(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s-a")
        _live_session(repo, "s-b")
        cmg.write_claude_md_write_grant("pm", "a's ask", session_id="s-a", cwd=str(repo))
        cmg.write_claude_md_write_grant("pm", "b's ask", session_id="s-b", cwd=str(repo))
        record_a = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s-a")
        record_b = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s-b")
        assert record_a["note"] == "a's ask"
        assert record_b["note"] == "b's ask"


class TestCheckClaudeMdWriteGrantFailClosed:
    def test_absent_file_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_malformed_json_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text("{not valid json", encoding="utf-8")
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_non_object_json_reads_ungranted(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        gfile = _grant_file(repo, "s1")
        gfile.parent.mkdir(parents=True, exist_ok=True)
        gfile.write_text("[1, 2, 3]", encoding="utf-8")
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
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
                    "note": "ask",
                }
            ),
            encoding="utf-8",
        )
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None  # still returned for audit/denial quoting

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
                    "note": "ask",
                }
            ),
            encoding="utf-8",
        )
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is not None

    def test_unreadable_file_reads_ungranted(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        cmg.write_claude_md_write_grant("pm", "ask", session_id="s1", cwd=str(repo))

        real_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self.name == "claude-md-write-grant.json":
                raise OSError("simulated unreadable file")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert granted is False
        assert record is None

    def test_unresolvable_session_reads_ungranted(self, tmp_path):
        granted, record = cmg.check_claude_md_write_grant(
            cwd=str(tmp_path / "not-a-repo"), session_id="s1"
        )
        assert granted is False
        assert record is None


# ---------------------------------------------------------------------------
# read_claude_md_write_grant — raw reader, no liveness gate
# ---------------------------------------------------------------------------


class TestReadClaudeMdWriteGrant:
    def test_returns_none_when_absent(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        assert cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s1") is None

    def test_reads_dead_session_grant_raw_without_liveness_gate(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        cmg.write_claude_md_write_grant("pm", "ask", session_id="s1", cwd=str(repo))
        _dead_session(repo, "s1")
        record = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="s1")
        assert record is not None
        assert record["note"] == "ask"


# ---------------------------------------------------------------------------
# Subagent resolvability — the LOAD-BEARING requirement this module's
# docstring names: a grant written via the default env-driven session
# resolution must be visible to a caller using that SAME default
# resolution path, because that is the exact path a C4 guard evaluation
# running inside a dispatched subagent's tool-call turn would use (one
# harness session, many tool-call turns — env vars are process-wide, not
# per-turn). If this were false, the escape hatch could not reach the
# path it exists for.
# ---------------------------------------------------------------------------


class TestSubagentResolvability:
    def test_default_resolution_write_then_default_resolution_check_agree(
        self, tmp_path, monkeypatch
    ):
        """Simulates the shape a dispatched-subagent guard evaluation
        actually sees: NO explicit session_id passed on either side — both
        the granting call (EM-inline turn) and the authorizing call
        (subagent-context guard turn) resolve via
        core.resolve_session_id(cwd), which reads the SAME
        COORDINATOR_SESSION_ID env var because it is one harness session,
        not two. This is the property the module docstring's
        SUBAGENT-RESOLVABILITY section commits to."""
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "harness-session-1")
        _live_session(repo, "harness-session-1")

        # EM-inline turn: no explicit session_id, resolves via env.
        ok = cmg.write_claude_md_write_grant(
            "pm", "PM said go ahead this session", cwd=str(repo)
        )
        assert ok is True

        # Subagent-context guard turn: also no explicit session_id — same
        # env var, same harness process, same resolution result.
        granted, record = cmg.check_claude_md_write_grant(cwd=str(repo))
        assert granted is True
        assert record["session_id"] == "harness-session-1"

    def test_default_resolution_matches_explicit_resolved_sid(self, tmp_path, monkeypatch):
        """The default-resolution grant is byte-identical (same session_id
        field) to one written with the sid resolved and passed explicitly
        — proves the two call shapes are not silently diverging."""
        repo = _make_repo(tmp_path)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "harness-session-2")
        _live_session(repo, "harness-session-2")

        resolved = core.resolve_session_id(str(repo))
        assert resolved == "harness-session-2"

        cmg.write_claude_md_write_grant("pm", "ask", cwd=str(repo))
        record_default = cmg.read_claude_md_write_grant(cwd=str(repo))
        record_explicit = cmg.read_claude_md_write_grant(
            cwd=str(repo), session_id="harness-session-2"
        )
        assert record_default == record_explicit


# ---------------------------------------------------------------------------
# CLI trampoline — grant | read | check, mirroring tier-u-grant-cli's shape
# ---------------------------------------------------------------------------


class TestCliMain:
    def test_grant_subcommand_writes_and_exits_zero(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "cli-session")
        _live_session(repo, "cli-session")
        rc = cmg.main(["grant", "pm", "PM said go ahead"])
        assert rc == 0
        record = cmg.read_claude_md_write_grant(cwd=str(repo), session_id="cli-session")
        assert record["note"] == "PM said go ahead"

    def test_check_subcommand_exit_code_reflects_grant_state(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "cli-session-2")
        _live_session(repo, "cli-session-2")
        assert cmg.main(["check"]) == 1  # no grant yet -> exit 1
        cmg.main(["grant", "pm", "ask"])
        assert cmg.main(["check"]) == 0  # granted -> exit 0

    def test_unknown_subcommand_exits_two(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        assert cmg.main(["bogus"]) == 2

    def test_grant_subcommand_bad_arity_exits_two(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        assert cmg.main(["grant", "pm"]) == 2

    def test_grant_subcommand_invalid_granted_by_exits_two(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.chdir(repo)
        assert cmg.main(["grant", "ceremony", "ask"]) == 2
