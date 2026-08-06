"""
Tests for coordinator_core.ops.fleet.archive_queue_entry — fleet.archive_queue_entry handler.

Coverage:
  (a) dry_run:true → {archived: False, dest: <computed path>}; no mutation.
  (b) dry_run:false → source gone, dest present, git log exact src+dst (--no-renames),
      git status clean.
  (c) YYYY-MM derivation: filename prefix primary, created: fallback.
  (d) Idempotent double-invocation (AC7): repeat dry_run:false on the same entry_path
      after the first archive → {archived: False, exit_code: 0}, mutates nothing further.
  (e) entry_path escaping state/improvement-queue/ is rejected (usage error).
  (f) repo_root is None → setup error, never a worktree-derivation guess.

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  Importing coordinator_core.ops.fleet.archive_queue_entry fires
  @register_op("fleet.archive_queue_entry") before any test that relies on registry state.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency. Handler called
directly with repo_root=fleet_repo.common_dir. Git mutations run ONLY inside the
throwaway fleet_repo fixture (a git repo under tmp_path) — never against the working repo.
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for fleet.archive_queue_entry. ----
import coordinator_core.ops.fleet.archive_queue_entry  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_queue_entry import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _seed_queue_entry(
    fleet_repo,
    name: str,
    *,
    title: str = "Test Queue Entry",
    created: str = "2026-01-01",
) -> Path:
    """Write and commit a state/improvement-queue/<name>.yaml (plain YAML, no fences).

    Local to this test file — conftest.py's FleetRepo has no improvement-queue seed
    helper and this chunk may not add one (shared fixture is out of scope).
    """
    path = fleet_repo.root / "state" / "improvement-queue" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = textwrap.dedent(f"""\
        created: {created}
        from_repo: test-repo
        queue_scope: project
        title: "{title}"
        body: "test body"
        status: open
    """)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", str(path)], cwd=str(fleet_repo.root), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"add queue entry {name}"],
        cwd=str(fleet_repo.root), check=True, capture_output=True,
    )
    return path


# ---------------------------------------------------------------------------
# Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_archive_queue_entry_registered():
    """fleet.archive_queue_entry must be in _REGISTRY after the import guard fires."""
    assert "fleet.archive_queue_entry" in _REGISTRY, (
        f"fleet.archive_queue_entry must be registered; registered ops: {sorted(_REGISTRY.keys())}"
    )


# ---------------------------------------------------------------------------
# (a) dry_run:true — preview only, no mutation
# ---------------------------------------------------------------------------


def test_dry_run_previews_dest_without_mutating(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-03-10-close-me.yaml")

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-03-10-close-me.yaml", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["archived"] is False
    assert result["dest"] == "archive/improvement-queue/2026-03/2026-03-10-close-me.yaml"

    # No mutation: source still present, working tree clean.
    src = fleet_repo.root / "state" / "improvement-queue" / "2026-03-10-close-me.yaml"
    assert src.is_file()
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (b) dry_run:false — act: git-mv + single commit, both src and dst in the commit
# ---------------------------------------------------------------------------


def test_act_moves_entry_and_commits(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-04-01-shipped.yaml")

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-04-01-shipped.yaml", "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["archived"] is True
    dest_rel = "archive/improvement-queue/2026-04/2026-04-01-shipped.yaml"
    assert result["dest"] == dest_rel

    src = fleet_repo.root / "state" / "improvement-queue" / "2026-04-01-shipped.yaml"
    dst = fleet_repo.root / dest_rel
    assert not src.exists()
    assert dst.is_file()
    assert fleet_repo.git_status_clean()

    log_result = subprocess.run(
        ["git", "log", "-1", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    )
    touched = [l for l in log_result.stdout.decode().strip().splitlines() if l.strip()]
    assert "state/improvement-queue/2026-04-01-shipped.yaml" in touched
    assert dest_rel in touched


# ---------------------------------------------------------------------------
# (c) YYYY-MM derivation: created: fallback when filename has no date prefix
# ---------------------------------------------------------------------------


def test_archive_month_falls_back_to_created_field(fleet_repo):
    _seed_queue_entry(fleet_repo, "no-date-prefix.yaml", created="2026-05-15")

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/no-date-prefix.yaml", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["dest"] == "archive/improvement-queue/2026-05/no-date-prefix.yaml"


# ---------------------------------------------------------------------------
# (d) Idempotent double-invocation (AC7)
# ---------------------------------------------------------------------------


def test_double_invocation_is_safe_no_op(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-06-01-double.yaml")
    params = {"entry_path": "state/improvement-queue/2026-06-01-double.yaml", "dry_run": False}

    first = _run(_handler(dict(params), repo_root=fleet_repo.common_dir))
    assert first["exit_code"] == 0
    assert first["archived"] is True

    head_after_first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().strip()

    second = _run(_handler(dict(params), repo_root=fleet_repo.common_dir))
    assert second["exit_code"] == 0
    assert second["archived"] is False
    assert second["dest"] == first["dest"]

    head_after_second = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().strip()
    assert head_after_second == head_after_first, "second invocation must not create a new commit"
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (e) Path-containment guard
# ---------------------------------------------------------------------------


def test_entry_path_escaping_queue_dir_is_rejected(fleet_repo):
    result = _run(_handler(
        {"entry_path": "../../etc/passwd", "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 2
    assert result["archived"] is False
    assert "escapes state/improvement-queue" in result["error"]


# ---------------------------------------------------------------------------
# (f) repo_root is None — fail loud, never guess the worktree
# ---------------------------------------------------------------------------


def test_repo_root_none_is_setup_error():
    result = _run(_handler(
        {"entry_path": "state/improvement-queue/whatever.yaml", "dry_run": True},
        repo_root=None,
    ))

    assert result["exit_code"] == 1
    assert result["archived"] is False
    assert "repo_root is None" in result["error"]


# ---------------------------------------------------------------------------
# (g) Batch mode (entry_paths) — 2026-08-06 F9 fix: ONE commit per sweep,
# never one per entry. See module docstring § Batch addition.
# ---------------------------------------------------------------------------


def test_batch_dry_run_previews_every_item_without_mutating(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-03-10-close-me.yaml")
    _seed_queue_entry(fleet_repo, "2026-03-11-close-me-too.yaml")

    result = _run(_handler(
        {
            "entry_paths": [
                "state/improvement-queue/2026-03-10-close-me.yaml",
                "state/improvement-queue/2026-03-11-close-me-too.yaml",
            ],
            "dry_run": True,
        },
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["archived"] is None
    assert result["dest"] is None
    assert len(result["items"]) == 2
    dests = {it["id"]: it["dest"] for it in result["items"]}
    assert dests["state/improvement-queue/2026-03-10-close-me.yaml"] == \
        "archive/improvement-queue/2026-03/2026-03-10-close-me.yaml"
    assert dests["state/improvement-queue/2026-03-11-close-me-too.yaml"] == \
        "archive/improvement-queue/2026-03/2026-03-11-close-me-too.yaml"
    assert all(it["error"] is None for it in result["items"])

    assert fleet_repo.git_status_clean()


def test_batch_act_archives_every_item_in_one_commit(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-04-01-shipped.yaml")
    _seed_queue_entry(fleet_repo, "2026-04-02-shipped-too.yaml")

    result = _run(_handler(
        {
            "entry_paths": [
                "state/improvement-queue/2026-04-01-shipped.yaml",
                "state/improvement-queue/2026-04-02-shipped-too.yaml",
            ],
            "dry_run": False,
        },
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert len(result["items"]) == 2
    assert all(it["archived"] is True and it["error"] is None for it in result["items"])

    dest_a = fleet_repo.root / "archive" / "improvement-queue" / "2026-04" / "2026-04-01-shipped.yaml"
    dest_b = fleet_repo.root / "archive" / "improvement-queue" / "2026-04" / "2026-04-02-shipped-too.yaml"
    assert dest_a.is_file()
    assert dest_b.is_file()
    assert fleet_repo.git_status_clean()

    # ONE commit for the whole batch — not one per entry.
    log_result = subprocess.run(
        ["git", "log", "--oneline", "-n", "5"],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    )
    subjects = log_result.stdout.decode().strip().splitlines()
    archive_commits = [l for l in subjects if "archive" in l and "improvement-queue" in l]
    assert len(archive_commits) == 1, f"expected exactly one archival commit, got: {archive_commits!r}"

    name_result = subprocess.run(
        ["git", "log", "-1", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    )
    touched = [l for l in name_result.stdout.decode().strip().splitlines() if l.strip()]
    assert "state/improvement-queue/2026-04-01-shipped.yaml" in touched
    assert "state/improvement-queue/2026-04-02-shipped-too.yaml" in touched
    assert "archive/improvement-queue/2026-04/2026-04-01-shipped.yaml" in touched
    assert "archive/improvement-queue/2026-04/2026-04-02-shipped-too.yaml" in touched


def test_batch_act_partial_failure_names_the_failed_item(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-04-05-ok.yaml")

    result = _run(_handler(
        {
            "entry_paths": [
                "state/improvement-queue/2026-04-05-ok.yaml",
                "state/improvement-queue/2026-04-06-never-existed.yaml",
            ],
            "dry_run": False,
        },
        repo_root=fleet_repo.common_dir,
    ))

    # A source that never existed is idempotent-skip (already-archived shape),
    # not a hard failure — but the still-valid item must still land, committed.
    assert result["exit_code"] == 0
    by_id = {it["id"]: it for it in result["items"]}
    assert by_id["state/improvement-queue/2026-04-05-ok.yaml"]["archived"] is True
    assert by_id["state/improvement-queue/2026-04-06-never-existed.yaml"]["archived"] is False
    assert by_id["state/improvement-queue/2026-04-06-never-existed.yaml"]["error"] is None


def test_batch_dry_run_flags_path_traversal_item_without_dropping_the_batch(fleet_repo):
    _seed_queue_entry(fleet_repo, "2026-04-10-fine.yaml")

    result = _run(_handler(
        {
            "entry_paths": [
                "state/improvement-queue/2026-04-10-fine.yaml",
                "../../etc/passwd",
            ],
            "dry_run": True,
        },
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 2
    by_id = {it["id"]: it for it in result["items"]}
    assert by_id["state/improvement-queue/2026-04-10-fine.yaml"]["error"] is None
    assert "escapes state/improvement-queue" in by_id["../../etc/passwd"]["error"]
    assert fleet_repo.git_status_clean()


def test_entry_path_and_entry_paths_are_mutually_exclusive(fleet_repo):
    result = _run(_handler(
        {
            "entry_path": "state/improvement-queue/a.yaml",
            "entry_paths": ["state/improvement-queue/b.yaml"],
            "dry_run": True,
        },
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 2
    assert "mutually exclusive" in result["error"]


def test_entry_paths_must_be_a_non_empty_list(fleet_repo):
    result = _run(_handler(
        {"entry_paths": [], "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["exit_code"] == 2
    assert "non-empty list" in result["error"]
