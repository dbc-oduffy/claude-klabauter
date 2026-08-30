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
