"""C11 (plan ``2026-08-27-a-pathspec-is-not-a-scope``) -- the third
granularity: a foreign hunk landed inside a file this session genuinely
owns.

Oracle: the live incident named in the plan and this chunk's own dispatch
brief -- ``git commit -- <one file>``, the sanctioned scoped form every
guard in this suite prints as its own remediation, swept a concurrent
session's uncommitted hunk into commit ``bf6099f85``. ``compute_scope``
resolved the path as this session's own, correctly, so Check 5's existing
warn loop stayed silent -- silence being exactly right by ITS rules, and
exactly wrong for this granularity. C10 records a whole-file ``sha256``
alongside a TOUCH; this exercises the read side (``check_validate_commit``)
that compares it against disk-now for every path already inside
``my_scope`` and refuses on a provable mismatch.

FAILURE DIRECTION under test, pinned by the brief: a RECORDED hash that
disagrees with disk is demonstrable and must refuse, naming the path. NO
recorded hash (the situation every current write channel is in today,
since nothing yet passes ``content_hash`` into ``append_event`` -- C10's
own docstring: "this chunk records only") is NOT demonstrable and must
never be read as foreignness -- it falls through to the pre-existing
my_scope/orphan behaviour unchanged.
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


def _claim_agent(
    root: str,
    agent_id: str,
    back_pointer_sid: str,
    event_sid: str,
    path: str,
    content_hash: Optional[str] = None,
) -> None:
    from coordinator_core.session import touch_record

    agent_dir = Path(root) / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text(
        back_pointer_sid + "\n", encoding="utf-8"
    )
    touch_record.append_event(
        touch_record.sink_path(agent_dir),
        session_id=event_sid,
        agent_id=agent_id,
        verb=touch_record.VERB_TOUCH,
        path=path,
        content_hash=content_hash,
    )


class TestCheckFiveForeignHunk:
    def test_matching_hash_no_deny(self, tmp_path):
        from coordinator_core.session.touch_record import compute_content_hash

        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        _claim(root, sid, "foo.txt", content_hash=compute_content_hash(tmp_path / "foo.txt"))

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_mismatched_hash_denies_naming_the_path(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("this session's own content\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        # Record a fingerprint for content DIFFERENT from what is on disk and
        _claim(root, sid, "foo.txt", content_hash="0" * 64)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "foreign hunk" in out["permissionDecisionReason"].lower()
        assert "foo.txt" in out["permissionDecisionReason"]

    def test_no_recorded_hash_falls_through_unchanged(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        _claim(root, sid, "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_foreign_unowned_file_still_warns_not_deny(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]

    def test_self_dispatched_agent_hash_allows_commit(self, tmp_path):
        from coordinator_core.session.touch_record import compute_content_hash

        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text(
            "the dispatched agent's own edit\n", encoding="utf-8"
        )
        _git(root, "add", "foo.txt")
        disk_hash = compute_content_hash(tmp_path / "foo.txt")

        _claim(root, sid, "foo.txt", content_hash="0" * 64)
        _claim_agent(root, "agent1", sid, sid, "foo.txt", content_hash=disk_hash)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_foreign_backpointer_agent_hash_still_denies(self, tmp_path):
        """Negative case for the defect 1 fix: an agent dir recording the
        matching disk-now hash but back-pointed at a DIFFERENT (peer)
        session must never forgive -- the self/other boundary must hold."""
        from coordinator_core.session.touch_record import compute_content_hash

        root = _init_repo(tmp_path)
        sid = "my-sess"
        peer_sid = "peer-sess"
        assert core.init(sid, cwd=root)
        assert core.init(peer_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text(
            "a peer's own dispatched edit\n", encoding="utf-8"
        )
        _git(root, "add", "foo.txt")
        disk_hash = compute_content_hash(tmp_path / "foo.txt")

        _claim(root, sid, "foo.txt", content_hash="0" * 64)
        _claim_agent(
            root, "agent2", peer_sid, peer_sid, "foo.txt", content_hash=disk_hash
        )

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "foreign hunk" in out["permissionDecisionReason"].lower()

    def test_deny_renders_pointer_for_em_audience(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("own content\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        _claim(root, sid, "foo.txt", content_hash="0" * 64)

        em_payload = {"session_id": sid}
        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root, payload=em_payload
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "foreign hunk" in reason.lower()
        assert "a foreign edit landed" not in reason.lower()

    def test_deny_renders_nothing_extra_for_subagent_audience(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("own content\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        _claim(root, sid, "foo.txt", content_hash="0" * 64)

        subagent_payload = {
            "session_id": sid,
            "agent_id": "abcdef0123456789",
        }
        em_payload = {"session_id": sid}

        subagent_result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root, payload=subagent_payload
        )
        em_result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root, payload=em_payload
        )
        assert subagent_result is not None and em_result is not None
        subagent_reason = subagent_result["hookSpecificOutput"][
            "permissionDecisionReason"
        ]
        em_reason = em_result["hookSpecificOutput"]["permissionDecisionReason"]
        assert len(subagent_reason) < len(em_reason)
