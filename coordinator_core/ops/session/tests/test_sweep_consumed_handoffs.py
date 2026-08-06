"""
coordinator_core.ops.session.tests.test_sweep_consumed_handoffs

Tests for the session.sweep_consumed_handoffs on-demand single-family op (C21,
2026-07-23) — the on-demand consumed-handoff archival CLI's underlying op, and the
dry_run parameter it adds to session.boot_sweep._sweep_consumed_handoffs.

Import guard: coordinator_core.ops.session.sweep_consumed_handoffs MUST be imported at
module load time to fire the @register_op("session.sweep_consumed_handoffs") side-effect
(lesson 2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) dry_run=true lists a would-archive candidate WITHOUT mutating anything — file not
      moved, working tree clean, no WARN marker written.
  (b) dry_run carries the 30-minute claimed_at recency floor — a just-claimed baton is
      excluded from the would-archive list and reported as skipped, not silently dropped.
  (c) dry_run carries the DR-084 non-heir consumed+in_flight skip-and-surface — reported
      in skipped with the "awaiting-adjudication-dr084" reason token, never silently
      swallowed, and the source file is left untouched.
  (d) a live (dry_run=false, the default) call actually archives a genuinely-eligible
      candidate — the op is not preview-only by construction, only by parameter.

Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C21

Negative-spec:
  - Does NOT re-test the full boot-path behavioral matrix (heir succession, two-repo
    routing, shipped_in stamping, partial-failure exit codes) — that matrix already lives
    in test_boot_sweep.py and this op delegates to the exact same
    _sweep_consumed_handoffs internal; re-asserting it here would be pure duplication.
    This file's scope is specifically the NEW dry_run parameter and the standalone op's
    own worktree-resolution wiring.
"""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.sweep_consumed_handoffs  # noqa: F401,E501 — fires register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.session.sweep_consumed_handoffs import _handler

_OP_NAME = "session.sweep_consumed_handoffs"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.sweep_consumed_handoffs @register_op did not fire"
)

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _run(coro):
    return asyncio.run(coro)


class ConsumedRepo:
    """Minimal temporary git repo for session.sweep_consumed_handoffs tests.

    Deliberately self-contained (not shared with test_boot_sweep.py's BootRepo) per the
    session test package convention — see that fixture's own docstring.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        args_list = list(args)
        if (
            len(args_list) >= 3
            and args_list[0] == "commit"
            and args_list[1] == "-m"
            and "Session-Id:" not in args_list[2]
        ):
            args_list[2] = f"{args_list[2]}\n\nSession-Id: {_DEFAULT_TEST_SESSION_ID}"
        return subprocess.run(
            ["git"] + args_list, cwd=str(self.root), capture_output=True, check=True,
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_handoff(self, name: str, status: str, extra_frontmatter: str = "") -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["title: \"Test Handoff\"", f"status: {status}", "created: 2026-01-01"]
        if extra_frontmatter.strip():
            fm_lines.append(extra_frontmatter.strip())
        fm_block = "\n".join(fm_lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def path_exists(self, repo_rel: str) -> bool:
        return (self.root / repo_rel).exists()

    def git_status_clean(self) -> bool:
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.root), capture_output=True,
        )
        return result.stdout.strip() == b""

    def warn_notes_exists(self) -> bool:
        return (self.root / "tasks" / "orphan-sweep-notes.md").exists()


@pytest.fixture
def consumed_repo(tmp_path) -> ConsumedRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True)

    _git("init", "-b", "main")
    _git("config", "user.email", "sweep-consumed-test@claude-klabauter.test")
    _git("config", "user.name", "Sweep Consumed Test")
    _git("config", "commit.gpgsign", "false")

    for d in ("state/handoffs",):
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return ConsumedRepo(repo_root)


# ---------------------------------------------------------------------------
# (a) dry_run previews without mutating
# ---------------------------------------------------------------------------


def test_dry_run_lists_candidate_without_mutating(consumed_repo):
    """An eligible (old, non-heir, non-in_flight) consumed handoff appears in the
    dry-run preview's archived (WOULD-archive) list, but the file is not moved, the
    working tree stays clean, and no WARN marker is written."""
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    consumed_repo.seed_handoff(
        "2026-07-01-eligible.md", "consumed",
        extra_frontmatter=f"claimed_at: {old.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
    cid = "state/handoffs/2026-07-01-eligible.md"

    result = _run(_handler({"dry_run": True}, repo_root=consumed_repo.common_dir))

    assert result["exit_code"] == 0, f"unexpected result: {result!r}"
    would_archive_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in would_archive_ids, (
        f"eligible candidate must appear in dry-run would-archive list; "
        f"got {result['consumed_handoffs']!r}"
    )

    assert consumed_repo.path_exists(cid), "dry_run must NOT move the source file"
    assert consumed_repo.git_status_clean(), "dry_run must NOT leave any uncommitted mutation"
    assert not consumed_repo.warn_notes_exists(), "dry_run must NOT write a WARN marker"


# ---------------------------------------------------------------------------
# (b) dry_run carries the 30-minute recency floor
# ---------------------------------------------------------------------------


def test_dry_run_recency_floor_excludes_freshly_claimed_baton(consumed_repo):
    """A handoff claimed 5 minutes ago must be excluded from the would-archive list and
    reported as skipped — proving the manual on-demand command does NOT foot-gun a live
    peer's just-claimed baton even in preview mode."""
    recent = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    consumed_repo.seed_handoff(
        "2026-07-01-fresh.md", "consumed",
        extra_frontmatter=f"claimed_at: {recent.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
    cid = "state/handoffs/2026-07-01-fresh.md"

    result = _run(_handler({"dry_run": True}, repo_root=consumed_repo.common_dir))

    would_archive_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in would_archive_ids, (
        "a freshly-claimed baton (within the 30-min recency floor) must NEVER appear "
        f"in the would-archive list; got {would_archive_ids!r}"
    )

    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert cid in skip_map, (
        f"freshly-claimed baton must be reported in skipped, not silently dropped; "
        f"got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    assert "recency floor" in skip_map[cid]
    assert consumed_repo.path_exists(cid)
    assert consumed_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (c) dry_run carries the DR-084 skip-and-surface, never silently swallowed
# ---------------------------------------------------------------------------


def test_dry_run_in_flight_skip_is_surfaced_not_silent(consumed_repo):
    """A non-heir consumed+in_flight candidate must be visible in skipped with the
    distinct DR-084 reason token, even under dry_run — never silently dropped."""
    consumed_repo.seed_handoff(
        "2026-07-01-inflight.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-01-inflight.md"

    result = _run(_handler({"dry_run": True}, repo_root=consumed_repo.common_dir))

    would_archive_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in would_archive_ids

    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert skip_map.get(cid) == "awaiting-adjudication-dr084", (
        f"in_flight skip must be surfaced with the DR-084 reason token; "
        f"got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    assert consumed_repo.path_exists(cid)
    assert consumed_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (d) a live call actually archives — dry_run is a parameter, not the only mode
# ---------------------------------------------------------------------------


def test_live_run_actually_archives_eligible_candidate(consumed_repo):
    """dry_run=false (the default) performs the real git-mv archival — proving this op
    is not preview-only by construction."""
    old = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    consumed_repo.seed_handoff(
        "2026-07-01-live.md", "consumed",
        extra_frontmatter=f"claimed_at: {old.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    )
    cid = "state/handoffs/2026-07-01-live.md"

    result = _run(_handler({}, repo_root=consumed_repo.common_dir))

    assert result["exit_code"] == 0, f"unexpected result: {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, f"eligible candidate must be archived; got {result!r}"
    assert not consumed_repo.path_exists(cid), "archived handoff must be moved out of state/handoffs/"
    assert consumed_repo.git_status_clean()
