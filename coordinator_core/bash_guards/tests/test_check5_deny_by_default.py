"""C6 (plan ``2026-08-27-a-pathspec-is-not-a-scope``) -- Check 5 flips from
warn-only to deny-by-default. ``COORDINATOR_SCOPE_STRICT_OFF`` is the escape
hatch back to warn-only; the un-overridden default is now strict.

BEHAVIOURAL, not a flag check: this suite proves the DENIAL actually fires
on a ``git commit -- <dir>`` carrying a foreign staged path inside, naming
that path -- not merely that some strict-mode variable is set. The plan
already shipped a field/comparator pair with nothing wired between them
once (C10/C11); this file exists so that failure mode cannot recur silently
here.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import core

# Spawns a real external `git` process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


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
    """Mirrors ``test_check5_foreign_hunk.py``'s own helper: pushes
    ``started_at`` an hour into the future so ``compute_scope``'s mtime
    fallback never auto-adopts a freshly-staged file into ``my_scope`` on
    its own -- every claim in this suite is made explicit through
    ``_claim``."""
    sdir = Path(root) / ".git" / "coordinator-sessions" / sid
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )


def _claim(root: str, sid: str, path: str) -> None:
    """Records ``path`` as claimed by ``sid`` through the canonical writer
    (``touch_record.append_event``), no content fingerprint -- this suite
    exercises the directory-pathspec/foreign-path granularity, not the
    foreign-hunk one (see ``test_check5_foreign_hunk.py``)."""
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


def _stage_dir_with_one_foreign_file(
    tmp_path: Path, root: str, sid: str, other_sid: str = None
) -> None:
    """A directory pathspec commit where ``sub/mine.txt`` is this session's
    own claimed work and ``sub/theirs.txt`` is staged foreign work -- the
    live sweep shape this chunk closes. C6b item 4: ``sub/theirs.txt`` must
    carry a REAL, PROVABLE peer claim (``other_sid``'s own touch record),
    not an unclaimed orphan -- the deny-by-default arm only fires on a
    provable owner (``dispatch_checks._owner_is_provable``); an orphan
    shape belongs to the warn-only arm, not this one."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "mine.txt").write_text("mine\n", encoding="utf-8")
    (tmp_path / "sub" / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _git(root, "add", "sub")
    _claim(root, sid, "sub/mine.txt")
    if other_sid is not None:
        _claim(root, other_sid, "sub/theirs.txt")


class TestCheckFiveDenyByDefault:
    def test_directory_commit_denies_foreign_staged_path_naming_it(self, tmp_path):
        """Un-overridden default: a directory pathspec commit carrying a
        provably peer-owned foreign staged path inside is REFUSED, naming
        that path."""
        root = _init_repo(tmp_path)
        sid, other_sid = "my-sess", "other-sess"
        assert core.init(sid, cwd=root)
        assert core.init(other_sid, cwd=root)
        _push_started_at_to_future(root, sid)
        _stage_dir_with_one_foreign_file(tmp_path, root, sid, other_sid)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "sub work" -- sub', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"
        assert "sub/theirs.txt" in out["permissionDecisionReason"]

    def test_directory_commit_all_owned_paths_still_passes(self, tmp_path):
        """Unmutated negative control: a directory pathspec commit whose
        staged paths this session genuinely owns must still pass -- the
        flip must not turn scoped work the session actually did into a
        false-positive deny."""
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)

        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "mine.txt").write_text("mine\n", encoding="utf-8")
        (tmp_path / "sub" / "also_mine.txt").write_text("also mine\n", encoding="utf-8")
        _git(root, "add", "sub")
        _claim(root, sid, "sub/mine.txt")
        _claim(root, sid, "sub/also_mine.txt")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "sub work" -- sub', sid, cwd=root
        )
        assert result is None

    def test_off_override_warns_instead_of_denying(self, tmp_path, monkeypatch):
        """``COORDINATOR_SCOPE_STRICT_OFF`` restores warn-only behaviour on
        the exact same foreign-path shape that denies by default above --
        the escape hatch this flip's recoverability depends on."""
        monkeypatch.setenv("COORDINATOR_SCOPE_STRICT_OFF", "1")
        root = _init_repo(tmp_path)
        sid = "my-sess"
        assert core.init(sid, cwd=root)
        _push_started_at_to_future(root, sid)
        _stage_dir_with_one_foreign_file(tmp_path, root, sid)

        result = dispatch_checks.check_validate_commit(
            'git commit -m "sub work" -- sub', sid, cwd=root
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "allow"
        assert "SCOPE:" in out["additionalContext"]
        assert "sub/theirs.txt" in out["additionalContext"]
