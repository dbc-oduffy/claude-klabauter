"""
Tests for coordinator_core.ops.fleet.archive_paper_trail — fleet.archive_paper_trail handler.

Coverage:
  (a) registration — fleet.archive_paper_trail lands in _REGISTRY on import.
  (b) dry_run:true — previews dest without mutating (src untouched, git clean).
  (c) dry_run:false — atomic rename: src gone, dest present at path with today's
      date, git log shows both src and dst (--no-renames), git status clean.
  (d) idempotent replay (AC7/AC12): re-dispatching the identical params after a
      successful archive is a vacuous no-op — archived:false, already_archived:true,
      no further mutation, git status clean.
  (e) same-day collision: dest already occupied while src still present is left
      untouched on both sides (no overwrite, no merge).
  (f) param validation: missing/blank run_id, topic_slug, wrong-typed dry_run,
      and a path-traversal attempt in either component all raise ValueError.
  (g) repo_root=None (engine misconfiguration) raises RuntimeError.

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  Importing coordinator_core.ops.fleet.archive_paper_trail fires
  @register_op("fleet.archive_paper_trail") before any test relies on registry state.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency.
Handler called directly with repo_root=fleet_repo.common_dir.
Git in these tests runs ONLY inside the throwaway fleet_repo fixture under
tmp_path — never against the working repo.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import date
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for fleet.archive_paper_trail. ----
import coordinator_core.ops.fleet.archive_paper_trail  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.archive_paper_trail import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Local seed helper — docs/research/ is not part of the shared fleet_repo
# skeleton, so this module seeds it directly (throwaway tmp_path repo only).
# ---------------------------------------------------------------------------

def _seed_workdir(fleet_repo, run_id: str, filename: str = "notes.md") -> Path:
    """Create + commit docs/research/{run_id}-workdir/{filename} in fleet_repo."""
    workdir = fleet_repo.root / "docs" / "research" / f"{run_id}-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / filename).write_text("paper trail content\n", encoding="utf-8")
    fleet_repo._git("add", str(workdir))
    fleet_repo._git("commit", "-m", f"seed workdir {run_id}")
    return workdir


def _git_log_names_no_renames(fleet_repo, n: int = 1):
    """Return file paths touched in the last n commits with rename-detection disabled.

    Mirrors test_prune_bugs.py's identical helper — needed so a git-mv rename
    shows both the deleted source AND the added destination (AC4/DR-211 D3
    scoped-pathspec verification).
    """
    result = subprocess.run(
        ["git", "log", f"-{n}", "--no-renames", "--name-only", "--format="],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    return [
        line
        for line in result.stdout.decode(errors="replace").strip().splitlines()
        if line.strip()
    ]


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# (a) Import-guard floor assertion
# ---------------------------------------------------------------------------

def test_archive_paper_trail_registered():
    """fleet.archive_paper_trail must be in _REGISTRY after the import guard fires."""
    fleet_ops = [k for k in _REGISTRY if k.startswith("fleet.")]
    assert len(fleet_ops) >= 1
    assert "fleet.archive_paper_trail" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) dry_run:true — preview only, no mutation
# ---------------------------------------------------------------------------

def test_dry_run_previews_dest_without_mutation(fleet_repo):
    run_id = "2026-07-22_120000"
    topic_slug = "my-topic"
    _seed_workdir(fleet_repo, run_id)

    result = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is False
    assert result["already_archived"] is False
    assert result["dest"] == f"docs/research/archive/{_today()}-{topic_slug}"

    assert fleet_repo.path_exists(f"docs/research/{run_id}-workdir")
    assert not fleet_repo.path_exists(result["dest"])
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (c) dry_run:false — atomic rename + single commit
# ---------------------------------------------------------------------------

def test_act_archives_workdir(fleet_repo):
    run_id = "2026-07-22_130000"
    topic_slug = "another-topic"
    _seed_workdir(fleet_repo, run_id, filename="result.md")

    result = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    expected_dest = f"docs/research/archive/{_today()}-{topic_slug}"
    assert result["archived"] is True
    assert result["already_archived"] is False
    assert result["dest"] == expected_dest

    assert not fleet_repo.path_exists(f"docs/research/{run_id}-workdir")
    assert fleet_repo.path_exists(f"{expected_dest}/result.md")
    assert fleet_repo.git_status_clean()

    touched = _git_log_names_no_renames(fleet_repo)
    assert f"docs/research/{run_id}-workdir/result.md" in touched
    assert f"{expected_dest}/result.md" in touched


# ---------------------------------------------------------------------------
# (d) Idempotent replay (AC7/AC12)
# ---------------------------------------------------------------------------

def test_double_invocation_is_safe_noop(fleet_repo):
    run_id = "2026-07-22_140000"
    topic_slug = "repeat-topic"
    _seed_workdir(fleet_repo, run_id)

    params = {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False}

    first = _run(_handler(dict(params), repo_root=fleet_repo.common_dir))
    assert first["archived"] is True
    assert first["already_archived"] is False

    second = _run(_handler(dict(params), repo_root=fleet_repo.common_dir))
    assert second["archived"] is False
    assert second["already_archived"] is True
    assert second["dest"] == first["dest"]

    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (e) Same-day collision — left untouched, no overwrite
# ---------------------------------------------------------------------------

def test_collision_leaves_both_sides_untouched(fleet_repo):
    run_id = "2026-07-22_150000"
    topic_slug = "collide-topic"
    _seed_workdir(fleet_repo, run_id)

    dest_rel = f"docs/research/archive/{_today()}-{topic_slug}"
    dest_path = fleet_repo.root / dest_rel
    dest_path.mkdir(parents=True, exist_ok=True)
    (dest_path / "already-there.md").write_text("pre-existing\n", encoding="utf-8")
    fleet_repo._git("add", str(dest_path))
    fleet_repo._git("commit", "-m", "seed pre-existing dest (collision fixture)")

    result = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is False
    assert result["already_archived"] is False
    assert result["dest"] == dest_rel

    # Neither side was touched.
    assert fleet_repo.path_exists(f"docs/research/{run_id}-workdir")
    assert fleet_repo.path_exists(f"{dest_rel}/already-there.md")
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (h) Empty-nested-subdir workdir (F3 regression) — no files anywhere under
#     src, but a nested empty subdirectory exists. Git never tracks empty
#     dirs, so this workdir is deliberately NOT committed via fleet_repo._git;
#     it is created directly on disk to reproduce the exact "zero files,
#     nonzero directory entries" shape a flat src.rmdir() cannot clear.
# ---------------------------------------------------------------------------
# Review: code-reviewer (F3) — a workdir containing only empty nested
# subdirectories (no files) previously stranded src permanently: the flat
# src.rmdir() call raised OSError (directory not empty) and was silently
# swallowed, so every subsequent call re-hit the identical failing branch.

def test_empty_nested_subdir_workdir_is_cleaned_up(fleet_repo):
    run_id = "2026-07-22_160000"
    topic_slug = "empty-nested-topic"
    workdir = fleet_repo.root / "docs" / "research" / f"{run_id}-workdir"
    (workdir / "nested" / "deeper").mkdir(parents=True, exist_ok=True)

    result = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is False
    assert result["already_archived"] is False
    assert not workdir.exists(), "empty nested-subdir workdir must be fully removed"

    # Re-invocation must converge to already_archived — not repeat the same
    # stranded-forever failure.
    second = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))
    assert second["already_archived"] is True


# ---------------------------------------------------------------------------
# (f) Param validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params",
    [
        {"run_id": "", "topic_slug": "x", "dry_run": False},
        {"run_id": None, "topic_slug": "x", "dry_run": False},
        {"run_id": "abc", "topic_slug": "", "dry_run": False},
        {"run_id": "abc", "topic_slug": "x", "dry_run": "false"},
        {"run_id": "../escape", "topic_slug": "x", "dry_run": False},
        {"run_id": "abc", "topic_slug": "../escape", "dry_run": False},
        {"run_id": "a/b", "topic_slug": "x", "dry_run": False},
    ],
)
def test_malformed_params_raise_value_error(fleet_repo, params):
    with pytest.raises(ValueError):
        _run(_handler(params, repo_root=fleet_repo.common_dir))


# ---------------------------------------------------------------------------
# (g) repo_root=None (engine misconfiguration)
# ---------------------------------------------------------------------------

def test_missing_repo_root_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(_handler(
            {"run_id": "abc", "topic_slug": "x", "dry_run": True},
            repo_root=None,
        ))
