"""
Tests for coordinator_core.ops.fleet.archive_release_accumulator —
fleet.archive_release_accumulator handler.

Coverage:
  (a) accumulator present + dry_run:true -> preview only, no mutation, dest computed.
  (b) accumulator present + dry_run:false -> git-mv + commit, archived=True, dest set.
  (c) no accumulator file -> vacuous no-op, archived=False, already_archived=True
      (covers both "never existed" and "already moved" via the same code path).
  (d) most-recent selection when multiple *-pending-release.md files exist.
  (e) idempotency (AC7): identical second dry_run:false call after a successful
      archive is a safe no-op (already_archived=True), never a crash/raise.
  (f) tag validation / dry_run type validation -> setup-error shape, no mutation.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency.
Handler called directly with repo_root=fleet_repo.common_dir.  All git activity
runs against the throwaway git repo the fleet_repo fixture creates under tmp_path —
never the working repo.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

# ---- Import guard: fires @register_op side-effect for fleet.archive_release_accumulator. ----
import coordinator_core.ops.fleet.archive_release_accumulator  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_release_accumulator import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Local helper — seed a pending-release accumulator file
# ---------------------------------------------------------------------------

def _seed_accumulator(fleet_repo, name: str, body: str = "## Added\n- thing\n"):
    """Write and commit a state/week-changelog/<name> accumulator file."""
    path = fleet_repo.root / "state" / "week-changelog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    fleet_repo._git("add", str(path))
    fleet_repo._git("commit", "-m", f"add accumulator {name}")
    return path


# ---------------------------------------------------------------------------
# Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_archive_release_accumulator_registered():
    """fleet.archive_release_accumulator must be in _REGISTRY after the import guard fires."""
    assert "fleet.archive_release_accumulator" in _REGISTRY, (
        f"fleet.archive_release_accumulator must be registered; got {sorted(_REGISTRY.keys())}"
    )


# ---------------------------------------------------------------------------
# (a) dry_run:true — preview only, no mutation
# ---------------------------------------------------------------------------


def test_dry_run_previews_dest_without_mutating(fleet_repo):
    _seed_accumulator(fleet_repo, "2026-07-01-pending-release.md")

    result = _run(_handler(
        {"tag": "v3.2.0", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is False
    assert result["already_archived"] is False
    assert result["dest"] == "archive/release-notes/2026-07-01-v3.2.0-pending-release.md"

    # Nothing mutated: source still present, dest absent, git status clean.
    assert fleet_repo.path_exists("state/week-changelog/2026-07-01-pending-release.md")
    assert not fleet_repo.path_exists("archive/release-notes/2026-07-01-v3.2.0-pending-release.md")
    assert fleet_repo.git_status_clean()


def test_dry_run_no_accumulator_reports_already_archived(fleet_repo):
    result = _run(_handler(
        {"tag": "v3.2.0", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result == {"archived": False, "dest": None, "already_archived": True}
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (b) dry_run:false — git-mv + commit
# ---------------------------------------------------------------------------


def test_act_archives_accumulator(fleet_repo):
    _seed_accumulator(fleet_repo, "2026-07-10-pending-release.md")

    result = _run(_handler(
        {"tag": "v3.3.0", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is True
    assert result["already_archived"] is False
    assert result["dest"] == "archive/release-notes/2026-07-10-v3.3.0-pending-release.md"

    assert not fleet_repo.path_exists("state/week-changelog/2026-07-10-pending-release.md")
    assert fleet_repo.path_exists("archive/release-notes/2026-07-10-v3.3.0-pending-release.md")
    assert fleet_repo.git_status_clean()


def test_act_commit_contains_both_src_and_dst(fleet_repo):
    """git log --no-renames must list BOTH src and dst (scoped-pathspec, AC4/DR-211 D3)."""
    _seed_accumulator(fleet_repo, "2026-07-11-pending-release.md")

    _run(_handler(
        {"tag": "v3.4.0", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    result = subprocess.run(
        ["git", "log", "-1", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    log_names = [
        line for line in result.stdout.decode(errors="replace").strip().splitlines() if line
    ]
    assert "state/week-changelog/2026-07-11-pending-release.md" in log_names
    assert "archive/release-notes/2026-07-11-v3.4.0-pending-release.md" in log_names


# ---------------------------------------------------------------------------
# (c) no accumulator -> vacuous no-op
# ---------------------------------------------------------------------------


def test_act_no_accumulator_is_vacuous_no_op(fleet_repo):
    result = _run(_handler(
        {"tag": "v3.5.0", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result == {"archived": False, "dest": None, "already_archived": True}
    assert fleet_repo.git_status_clean()


def test_act_no_week_changelog_dir_is_vacuous_no_op(fleet_repo, tmp_path):
    """A repo with no state/week-changelog/ dir at all must not raise."""
    import shutil

    changelog_dir = fleet_repo.root / "state" / "week-changelog"
    if changelog_dir.exists():
        shutil.rmtree(changelog_dir)

    result = _run(_handler(
        {"tag": "v3.5.1", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result == {"archived": False, "dest": None, "already_archived": True}


# ---------------------------------------------------------------------------
# (d) most-recent selection among multiple accumulators
# ---------------------------------------------------------------------------


def test_act_selects_most_recent_accumulator(fleet_repo):
    _seed_accumulator(fleet_repo, "2026-06-01-pending-release.md")
    _seed_accumulator(fleet_repo, "2026-07-15-pending-release.md")

    result = _run(_handler(
        {"tag": "v4.0.0", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is True
    assert result["dest"] == "archive/release-notes/2026-07-15-v4.0.0-pending-release.md"
    # The older accumulator is left untouched — only one pending-release accumulator
    # is archived per call, matching the SKILL.md source step (singular $PENDING_RELEASE).
    assert fleet_repo.path_exists("state/week-changelog/2026-06-01-pending-release.md")
    assert not fleet_repo.path_exists("state/week-changelog/2026-07-15-pending-release.md")


# ---------------------------------------------------------------------------
# (e) Idempotency (AC7)
# ---------------------------------------------------------------------------


def test_idempotent_replay_same_params(fleet_repo):
    """Second dry_run:false call with identical params after a successful archive
    is a safe no-op — never raises, reports already_archived=True."""
    _seed_accumulator(fleet_repo, "2026-07-12-pending-release.md")

    params = {"tag": "v3.6.0", "dry_run": False}

    result1 = _run(_handler(params, repo_root=fleet_repo.common_dir))
    assert result1["archived"] is True

    result2 = _run(_handler(params, repo_root=fleet_repo.common_dir))
    assert result2 == {"archived": False, "dest": None, "already_archived": True}
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (f) Validation — tag / dry_run type
# ---------------------------------------------------------------------------


def test_missing_tag_is_setup_error(fleet_repo):
    _seed_accumulator(fleet_repo, "2026-07-13-pending-release.md")

    result = _run(_handler(
        {"tag": "", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result == {"archived": False, "dest": None, "already_archived": False}
    # Nothing mutated on a setup error.
    assert fleet_repo.path_exists("state/week-changelog/2026-07-13-pending-release.md")
    assert fleet_repo.git_status_clean()


def test_non_bool_dry_run_is_setup_error(fleet_repo):
    result = _run(_handler(
        {"tag": "v3.7.0", "dry_run": "false"},
        repo_root=fleet_repo.common_dir,
    ))

    assert result == {"archived": False, "dest": None, "already_archived": False}
