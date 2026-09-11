"""C12 (three independent sessions hit the same drip, 2026-09-11) -- Check 5's
staged-file walk used to ``return _deny(...)`` on the FIRST offending path
across all four arms (foreign hunk, vanished-from-disk, contested, unclaimed),
so a multi-path commit surfaced exactly one more name per clearing attempt
with no way to see the at-risk set's size (measured: a 311-path plan-blitz
landing).

This suite proves the walk now finishes and reports the WHOLE at-risk set in
one denial, with each arm's own distinct reason text preserved, while a
single-offender commit and a clean commit are unaffected.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external `git` process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags())


def _init_repo(tmp_path: Path) -> str:
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _push_started_at_to_future(root: str, sid: str) -> None:
    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )


def _claim(root: str, sid: str, path: str, content_hash: Optional[str] = None) -> None:
    from coordinator_core.session import touch_record

    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sdir),
        session_id=sid,
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path=path,
        content_hash=content_hash,
    )


class TestBatchedScopeDenial:
    def test_multi_path_deny_names_every_offender_once(self, tmp_path):
        """A foreign-hunk file (own-recorded hash mismatch) and a
        peer-owned foreign file staged together: both names must appear in
        ONE deny, and the arms must stay distinct (own reason text intact
        for each)."""
        from coordinator_core.session.touch_record import compute_content_hash

        root = _init_repo(tmp_path)
        sid, peer_sid = "my-sess", "peer-sess"
        assert core.init(sid, cwd=root)
        assert core.init(peer_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "hunk.txt").write_text("this session's own content\n", encoding="utf-8")
        (tmp_path / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        _git(root, "add", "hunk.txt", "theirs.txt")
        # foreign-hunk: recorded hash disagrees with disk-now.
        _claim(root, sid, "hunk.txt", content_hash="0" * 64)
        # unclaimed-by-peer: a real, provable peer claim.
        _claim(root, peer_sid, "theirs.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "batch"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]

        assert "hunk.txt" in reason
        assert "theirs.txt" in reason
        assert "foreign hunk" in reason.lower()
        assert "not in this session's touch list" in reason
        assert "2 staged paths failed validation" in reason

    def test_single_offender_reads_naturally_unwrapped(self, tmp_path):
        """A lone offender must render EXACTLY its own arm text -- no batch
        header, no wrapping -- so a one-file commit's denial is unchanged
        from before this change."""
        root = _init_repo(tmp_path)
        sid, peer_sid = "my-sess", "peer-sess"
        assert core.init(sid, cwd=root)
        assert core.init(peer_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        _git(root, "add", "theirs.txt")
        _claim(root, peer_sid, "theirs.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "single"', sid, cwd=root
        )
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "staged paths failed validation" not in reason
        assert reason.startswith("BLOCKED (strict scope): theirs.txt is staged but not in")

    def test_clean_staged_set_still_passes(self, tmp_path):
        """Negative control: every staged path genuinely owned by this
        session must still pass with no denial, unaffected by the batching
        change."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "mine.txt").write_text("mine\n", encoding="utf-8")
        (tmp_path / "also_mine.txt").write_text("also mine\n", encoding="utf-8")
        _git(root, "add", "mine.txt", "also_mine.txt")
        _claim(root, sid, "mine.txt")
        _claim(root, sid, "also_mine.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "clean"', sid, cwd=root
        )
        assert result is None
