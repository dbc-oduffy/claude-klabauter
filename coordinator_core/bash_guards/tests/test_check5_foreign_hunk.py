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
    """Mirrors ``test_check_validate_commit.py``'s own helper: pushes
    ``started_at`` an hour into the future so ``compute_scope``'s mtime
    fallback never auto-adopts a freshly-staged file into ``my_scope`` on
    its own -- every claim in this suite is made explicit through
    ``_claim``/``_claim_with_hash``."""
    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )


def _claim(root: str, sid: str, path: str, content_hash: Optional[str] = None) -> None:
    """Record ``path`` as claimed by ``sid`` through the canonical writer
    (``touch_record.append_event``), optionally carrying a C10 content
    fingerprint -- mirrors ``test_check_validate_commit.py``'s own
    ``_claim`` helper, extended with the one field this chunk consumes."""
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
    """Record ``path`` under ``.agents/<agent_id>/touch-record.jsonl``, with
    ``em-session-id.txt`` back-pointed at ``back_pointer_sid`` -- mirrors
    ``track_touched_files.py::_handler``'s own agent-keyed write shape (see
    ``test_check_validate_commit.py``'s dispatched-agent test), extended
    with this chunk's C10 ``content_hash`` field."""
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
        """The recorded fingerprint matches disk-now -- a genuine own-write,
        no foreign edit. Must not deny, must not even warn (this path is
        already in ``my_scope``)."""
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
        """The `bf6099f85` shape: this session recorded a fingerprint for one
        content, then the on-disk content diverged (a peer's foreign edit,
        modeled here directly since two real writers racing one file is not
        reproducible deterministically in a single-process test) before the
        commit. This is the ONE case that must refuse, naming the path."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("this session's own content\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        # Record a fingerprint for content DIFFERENT from what is on disk and
        # staged now -- the foreign-edit shape: this session's own last
        # recorded write no longer matches disk-now.
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
        """The state every write channel is in TODAY (C10 records only; no
        caller yet passes ``content_hash``). A hash-less TOUCH must NEVER be
        read as foreign -- it is simply not demonstrable. Falls through to
        the pre-existing my_scope behaviour: no warning, no deny."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _git(root, "add", "foo.txt")
        _claim(root, sid, "foo.txt")  # no content_hash

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None

    def test_foreign_unowned_file_still_warns_not_deny(self, tmp_path):
        """Regression guard: a path this session never claimed at all must
        keep going through the pre-existing orphan/foreign-staged advisory
        path, entirely untouched by this chunk's fingerprint comparison
        (which only ever runs for a path already inside ``my_scope``)."""
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
        """Defect 1 fix: the EM's own recorded fingerprint is stale relative
        to disk because a dispatched subagent this EM's own session spawned
        overwrote the file and recorded ITS OWN hash under the agent-keyed
        sink `.agents/<aid>/touch-record.jsonl`, back-pointed at THIS
        session via `em-session-id.txt`. That is this session's own
        provenance -- must ALLOW, not read as a foreign edit."""
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

        # The EM's own record is now stale -- it never saw the agent's edit.
        _claim(root, sid, "foo.txt", content_hash="0" * 64)
        # The dispatched agent's own PostToolUse write, back-pointed at
        # THIS session, recorded the content that is actually on disk now.
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
        # Back-pointed at the PEER, not this session -- not this session's
        # own fan-out, must still deny.
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
        """Defect 2 fix: this deny is the only guard in its family that
        used to end with '...or coordinate with whoever else touched it'
        and name no route out. It must now splice the audience-gated
        pointer (``operator_override_note``) for a positively-resolved EM
        payload."""
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
        # Accuracy fix (defect 1's last bullet): the text must not assert a
        # foreign edit landed when the guard cannot demonstrate that -- it
        # only names the mismatch itself.
        assert "a foreign edit landed" not in reason.lower()

    def test_deny_renders_nothing_extra_for_subagent_audience(self, tmp_path):
        """Same deny, but with a payload that resolves to a subagent
        audience (a non-empty ``agent_id``) -- ``operator_override_note``
        returns '' for any non-EM audience, so no pointer is appended."""
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
