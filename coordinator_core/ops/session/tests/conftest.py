"""Shared pytest fixtures for coordinator_core.ops.session tests.

Primary fixture: ``session_repo`` — a minimal temporary git repository with a
.git/coordinator-sessions/ directory tree, used by session.reap tests.

The ``SessionRepo`` helper provides:
- ``common_dir``                             — Path to git common dir (.git)
- ``sessions_dir``                           — Path to .git/coordinator-sessions/
- ``seed_session(sid, hours_ago)``           — Create a session dir with meta.json
- ``seed_agent(aid, hours_ago)``             — Create an agent dir with a touched.txt
- ``seed_claim(claim_type, claim_name)``     — Create a claim dir in a claim subdirectory
- ``touch_last_reap(hours_ago)``             — Write .last-reap marker with given mtime

Class-B tests (session.reap) do not perform git commits — they test directory-level
mutations inside .git/coordinator-sessions/ only. No git status assertions are made.

Negative-spec:
- Does NOT extend FleetRepo — session tests have a different substrate; importing
  fleet's conftest would couple Class-B tests to git-tracked artifact conventions.
- Does NOT track coordinator-sessions/ in git — it is untracked substrate per the
  Class-B safety spec (DR-211 git bounds do NOT apply to this package).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# SessionRepo helper
# ---------------------------------------------------------------------------


def _past_iso(hours_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp N hours in the past."""
    ts = time.time() - (hours_ago * 3600)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat()


class SessionRepo:
    """Wrapper around a temporary git repository for session op tests.

    Do not instantiate directly — use the ``session_repo`` fixture.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_dir = root / ".git"
        self.sessions_dir = self.git_dir / "coordinator-sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def common_dir(self) -> Path:
        """Absolute path to the git common dir (.git for a non-worktree repo)."""
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    # -- Seed helpers --------------------------------------------------------

    def seed_session(self, sid: str, hours_ago: float) -> Path:
        """Create a session dir with meta.json whose last_activity is N hours in the past.

        Returns the path to the session directory.
        """
        sdir = self.sessions_dir / sid
        sdir.mkdir(parents=True, exist_ok=True)
        meta = {"last_activity": _past_iso(hours_ago)}
        (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return sdir

    def seed_agent(self, aid: str, hours_ago: float) -> Path:
        """Create an agent dir under .agents/ with a touched.txt whose mtime is N hours ago.

        Returns the path to the agent directory.
        """
        agents_dir = self.sessions_dir / ".agents"
        adir = agents_dir / aid
        adir.mkdir(parents=True, exist_ok=True)
        touched = adir / "touched.txt"
        touched.write_text("touched", encoding="utf-8")
        past_time = time.time() - (hours_ago * 3600)
        os.utime(str(touched), (past_time, past_time))
        return adir

    def seed_claim(self, claim_type: str, claim_name: str) -> Path:
        """Create a claim dir under <sessions_dir>/<claim_type>/<claim_name>/.

        Returns the path to the claim directory.
        Claim types mirror _CLAIM_SUBDIRS: handoff-claims, memo-claims, plan-claims.
        """
        claim_dir = self.sessions_dir / claim_type / claim_name
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / "claim.json").write_text("{}", encoding="utf-8")
        return claim_dir

    def touch_last_reap(self, hours_ago: float = 0.0) -> Path:
        """Write (or update) the .last-reap cadence marker with an mtime N hours in the past.

        hours_ago=0.0 means now (i.e., reap just ran), which will cause the
        cadence gate to fire on the next handler call.
        Returns the path to the marker file.
        """
        marker = self.sessions_dir / ".last-reap"
        marker.touch()
        if hours_ago > 0.0:
            past_time = time.time() - (hours_ago * 3600)
            os.utime(str(marker), (past_time, past_time))
        return marker

    def archive_dest_exists(self, sid: str) -> bool:
        """Return True iff a .archive/<sid>-* directory exists for the given session id."""
        archive_root = self.sessions_dir / ".archive"
        if not archive_root.exists():
            return False
        return any(
            d.name.startswith(f"{sid}-")
            for d in archive_root.iterdir()
            if d.is_dir()
        )

    def agent_archive_exists(self, aid: str) -> bool:
        """Return True iff a .archive/_agents-<aid>-* directory exists."""
        archive_root = self.sessions_dir / ".archive"
        if not archive_root.exists():
            return False
        return any(
            d.name.startswith(f"_agents-{aid}-")
            for d in archive_root.iterdir()
            if d.is_dir()
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def session_repo(tmp_path) -> SessionRepo:
    """Provide a temporary git repository with .git/coordinator-sessions/.

    The repo has:
    - git config user.email / user.name / commit.gpgsign=false set
    - An initial commit so git rev-parse --git-common-dir works
    - An empty coordinator-sessions/ directory inside .git/

    Usage in tests::

        def test_something(session_repo):
            session_repo.seed_session("sid-abc123", hours_ago=48)
            # ... call handler ...
            assert session_repo.archive_dest_exists("sid-abc123")
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo_root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "session-test@claude-klabauter.test")
    _git("config", "user.name", "Session Test")
    _git("config", "commit.gpgsign", "false")

    # Initial commit so git rev-parse commands work.
    (repo_root / "README.md").write_text("test repo\n", encoding="utf-8")
    _git("add", "README.md")
    _git("commit", "-m", "chore: initial commit")

    return SessionRepo(repo_root)
