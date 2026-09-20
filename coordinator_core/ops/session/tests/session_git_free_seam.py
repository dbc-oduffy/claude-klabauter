"""
coordinator_core.ops.session.tests.session_git_free_seam

C7 of docs/plans/2026-08-13-archive-family-coverage-restoration.md (AC3).
Rule-setting only — this module ports no test. It extends C1's
git-free-helper pattern (coordinator_core/ops/fleet/tests/
archive_git_free_seam.py) to Class 1's per-test-conftest expression:
`session_repo`, `fleet_repo`'s sibling named in
state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md line 26
alongside `handoff_repo` and `norm_repo`. A plain, explicitly-imported
helper module — NEVER a conftest.py, NEVER autouse — for the same reason
C1 gives: the 2026-08-07 incident was a conftest fixture running real git
ambiently, per test, on a machine running 50-70 concurrent LLM sessions.
Nothing here may be discovered or applied without a test explicitly
writing
`from coordinator_core.ops.session.tests.session_git_free_seam import ...`.

AC3 — the written discriminator, restated for this package
-------------------------------------------------------------
The original `session_repo` fixture (recovered verbatim from the excision
ledger's parent sha, `git show 6f0e89044:coordinator_core/ops/session/
tests/conftest.py`) ran a real `git init -b main` plus one commit, per
test, to satisfy exactly one downstream need: `SessionRepo.common_dir`
shelled out to `git rev-parse --path-format=absolute --git-common-dir` to
resolve the git common dir.

That fixture's own docstring already states the discriminator this module
applies, unchanged: "Class-B tests (session.reap) do not perform git
commits — they test directory-level mutations inside
.git/coordinator-sessions/ only. No git status assertions are made." Every
seed/assert helper (`seed_session`, `seed_agent`, `seed_claim`,
`touch_last_reap`, `archive_dest_exists`, `agent_archive_exists`) reads
and writes plain files under a directory tree — none inspects git's index,
HEAD, or object database. The one git call in the whole fixture existed
only to answer "what path is my git common dir", which for a fixture that
builds `repo_root/.git/coordinator-sessions/` itself is `repo_root / ".git"`
by construction — no subprocess needed to learn a fact the fixture already
knows because it just created it.

This governs every session-package test ported off `session_repo`: if a
test's assertion is about `.git/coordinator-sessions/` directory content
(what `seed_*`/`touch_last_reap` produced, or what a handler mutated
there), it is git-free and belongs on `GitFreeSessionRepo` below. A
session test whose assertion is instead about git's own state — an actual
commit SHA, the index, or `git status` output — is real-git by the same
class-1 test in C1's discriminator and does NOT belong here; C7 finds no
such test in this package's known population (per C6b) but the question
still applies per-test, not per-package.

Population B's second expression — the module-local inline `_git()`
helper — is a distinct package's concern
(coordinator/bin/tests/bin_git_free_seam.py); same mechanism, same
question, not re-derived here per this row's body.
"""

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
    """Git-free stand-in for the original `SessionRepo` (see this module's
    docstring). Same seed/assert surface, built directly on a plain
    `tmp_path`-derived directory — no `git init`, no subprocess, no
    `.git` beyond the plain directory this class creates itself.

    Do not instantiate directly — use `make_git_free_session_repo`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_dir = root / ".git"
        self.sessions_dir = self.git_dir / "coordinator-sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def common_dir(self) -> Path:
        """The git common dir, resolved without a subprocess.

        The original fixture shelled out to `git rev-parse
        --git-common-dir` to learn this; a fixture that builds
        `root/.git` itself already knows the answer.
        """
        return self.git_dir.resolve()

    # -- Seed helpers, unchanged from the original SessionRepo --------------

    def seed_session(self, sid: str, hours_ago: float) -> Path:
        """Create a session dir with meta.json whose last_activity is N hours in the past."""
        sdir = self.sessions_dir / sid
        sdir.mkdir(parents=True, exist_ok=True)
        meta = {"last_activity": _past_iso(hours_ago)}
        (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return sdir

    def seed_agent(self, aid: str, hours_ago: float) -> Path:
        """Create an agent dir under .agents/ with a touched.txt whose mtime is N hours ago."""
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
        """Write (or update) the .last-reap cadence marker with an mtime N hours in the past."""
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


def make_git_free_session_repo(tmp_path: Path) -> GitFreeSessionRepo:
    """Build a `GitFreeSessionRepo` rooted under `tmp_path`.

    Explicit call, never a fixture — a test that needs one writes
    `session_repo = make_git_free_session_repo(tmp_path)` itself. Spawns
    no process; see this module's docstring for the discriminator that
    makes this safe for Class-B session tests.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    return GitFreeSessionRepo(repo_root)
