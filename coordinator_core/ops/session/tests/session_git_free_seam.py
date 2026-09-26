
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _past_iso(hours_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp N hours in the past."""
    ts = time.time() - (hours_ago * 3600)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.isoformat()


class GitFreeSessionRepo:

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_dir = root / ".git"
        self.sessions_dir = self.git_dir / "coordinator-sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def common_dir(self) -> Path:
        return self.git_dir.resolve()


    def seed_session(self, sid: str, hours_ago: float) -> Path:
        sdir = self.sessions_dir / sid
        sdir.mkdir(parents=True, exist_ok=True)
        meta = {"last_activity": _past_iso(hours_ago)}
        (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return sdir

    def seed_agent(self, aid: str, hours_ago: float) -> Path:
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

        Claim types mirror _CLAIM_SUBDIRS: handoff-claims, memo-claims, plan-claims.
        """
        claim_dir = self.sessions_dir / claim_type / claim_name
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / "claim.json").write_text("{}", encoding="utf-8")
        return claim_dir

    def touch_last_reap(self, hours_ago: float = 0.0) -> Path:
        marker = self.sessions_dir / ".last-reap"
        marker.touch()
        if hours_ago > 0.0:
            past_time = time.time() - (hours_ago * 3600)
            os.utime(str(marker), (past_time, past_time))
        return marker

    def archive_dest_exists(self, sid: str) -> bool:
        archive_root = self.sessions_dir / ".archive"
        if not archive_root.exists():
            return False
        return any(
            d.name.startswith(f"{sid}-")
            for d in archive_root.iterdir()
            if d.is_dir()
        )

    def agent_archive_exists(self, aid: str) -> bool:
        archive_root = self.sessions_dir / ".archive"
        if not archive_root.exists():
            return False
        return any(
            d.name.startswith(f"_agents-{aid}-")
            for d in archive_root.iterdir()
            if d.is_dir()
        )


def make_git_free_session_repo(tmp_path: Path) -> GitFreeSessionRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    return GitFreeSessionRepo(repo_root)
