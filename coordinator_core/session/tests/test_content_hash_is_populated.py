"""C12 (plan ``2026-08-27-a-pathspec-is-not-a-scope``) -- populate
``content_hash`` at the write sites, so C11 (``dispatch_checks.
check_validate_commit``'s foreign-hunk comparator) has something to compare.

C10 shipped the ``TouchEvent.content_hash`` field; C11 shipped the comparator
that reads it back; neither shipped a production caller that PASSES
``content_hash`` into ``touch_record.append_event``, so C11's refusal branch
was unreachable on every real commit. This chunk wires two write sites:

  - ``coordinator_core.hooks.track_touched_files`` (the Write/Edit hook path)
  - ``touch_record.reconcile_untouched_bash_writes`` (the C2 bash-write
    reconciler)

Acceptance is BEHAVIOURAL, not structural (this chunk's own dispatch brief):
a test that writes a file through the recording path, mutates it out of
band, stages it, and asserts C11's refusal actually FIRES -- not merely that
the ``hash`` field is present on the encoded line.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import core, touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro):
    return asyncio.run(coro)


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


class TestHookPathPopulatesHashAndRefusalFires:

    def test_own_edit_then_foreign_mutation_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        from coordinator_core.hooks.track_touched_files import _handler

        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("this session's own content\n", encoding="utf-8")
        _run(_handler(
            {"session_id": sid, "tool_name": "Write", "file_path": "foo.txt", "agent_id": ""},
            repo_root=root,
        ))

        sink = Path(root) / ".git" / "coordinator-sessions" / sid / "touch-record.jsonl"
        events = [
            touch_record.decode_line(line)
            for line in touch_record.iter_complete_lines(sink.read_bytes())
        ]
        own_event = next(e for e in events if e.path == "foo.txt")
        assert own_event.content_hash is not None

        (tmp_path / "foo.txt").write_text("a peer's foreign edit\n", encoding="utf-8")
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "foreign hunk" in out["permissionDecisionReason"].lower()
        assert "foo.txt" in out["permissionDecisionReason"]

    def test_own_edit_unmutated_does_not_deny(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        from coordinator_core.hooks.track_touched_files import _handler

        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "foo.txt").write_text("hello\n", encoding="utf-8")
        _run(_handler(
            {"session_id": sid, "tool_name": "Write", "file_path": "foo.txt", "agent_id": ""},
            repo_root=root,
        ))
        _git(root, "add", "foo.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add foo"', sid, cwd=root
        )
        assert result is None


class TestBashReconcilerPopulatesHashAndRefusalFires:

    def test_reconciled_write_then_foreign_mutation_is_refused(self, tmp_path):
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        session_dir = Path(root) / ".git" / "coordinator-sessions" / sid

        (tmp_path / "bar.txt").write_text("this session's own bash write\n", encoding="utf-8")

        sink = touch_record.sink_path(session_dir)
        attributed = touch_record.reconcile_untouched_bash_writes(
            sink,
            session_id=sid,
            agent_id=None,
            session_dir=session_dir,
            candidate_paths=["bar.txt"],
            cwd=root,
        )
        assert attributed == ["bar.txt"]

        events = [
            touch_record.decode_line(line)
            for line in touch_record.iter_complete_lines(sink.read_bytes())
        ]
        own_event = next(e for e in events if e.path == "bar.txt")
        assert own_event.content_hash is not None

        (tmp_path / "bar.txt").write_text("a peer's foreign edit\n", encoding="utf-8")
        _git(root, "add", "bar.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "add bar"', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "foreign hunk" in out["permissionDecisionReason"].lower()
        assert "bar.txt" in out["permissionDecisionReason"]
