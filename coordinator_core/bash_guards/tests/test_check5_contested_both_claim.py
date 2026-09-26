
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

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


def _claim(root: str, sid: str, path: str) -> None:
    from coordinator_core.session import touch_record

    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sdir),
        session_id=sid,
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path=path,
    )


def _stage_contested(tmp_path: Path, root: str, sid: str, peer_sid: str) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mine.txt").write_text("mine\n", encoding="utf-8")
    (tmp_path / "sub" / "contested.txt").write_text("mine too\n", encoding="utf-8")
    _git(root, "add", "sub")
    _claim(root, sid, "sub/mine.txt")
    _claim(root, sid, "sub/contested.txt")
    _claim(root, peer_sid, "sub/contested.txt")


class TestContestedBothClaim:
    def test_deny_names_the_contention_not_an_absent_claim(self, tmp_path):
        root = _init_repo(tmp_path)
        sid, peer_sid = "my-sess", "peer-sess"
        assert core.init(sid, cwd=root)
        assert core.init(peer_sid, cwd=root)
        _push_started_at_to_future(root, sid)
        _stage_contested(tmp_path, root, sid, peer_sid)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "sub work" -- sub', sid, cwd=root
        )

        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "sub/contested.txt" in reason
        assert "claimed by BOTH" in reason, reason
        assert "not in this session's touch list" not in reason, reason

    def test_deny_does_not_offer_the_remedy_that_cannot_clear_it(self, tmp_path):
        root = _init_repo(tmp_path)
        sid, peer_sid = "my-sess", "peer-sess"
        assert core.init(sid, cwd=root)
        assert core.init(peer_sid, cwd=root)
        _push_started_at_to_future(root, sid)
        _stage_contested(tmp_path, root, sid, peer_sid)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "sub work" -- sub', sid, cwd=root
        )

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "record it as touched first" not in reason, reason

    def test_unclaimed_foreign_path_keeps_the_original_message(self, tmp_path):
        root = _init_repo(tmp_path)
        sid, peer_sid = "my-sess", "peer-sess"
        assert core.init(sid, cwd=root)
        assert core.init(peer_sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "mine.txt").write_text("mine\n", encoding="utf-8")
        (tmp_path / "sub" / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        _git(root, "add", "sub")
        _claim(root, sid, "sub/mine.txt")
        _claim(root, peer_sid, "sub/theirs.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "sub work" -- sub', sid, cwd=root
        )

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "sub/theirs.txt" in reason
        assert "not in this session's touch list" in reason, reason
        assert "claimed by BOTH" not in reason, reason
