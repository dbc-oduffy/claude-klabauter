"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_trailer_divergence

Covers the C10 fix (docs/plans/2026-07-27-computed-commit-mechanism-
selection.md § C10): `wsc_tail._derive_trailers()` used to pre-stage
`stage_paths` unconditionally (`git add -- stage_paths` against the SHARED
index) before reading the staged diff for `commit.anchors`. On a path with
deliberate PARTIAL-HUNK staging (index differs from HEAD *and* worktree
differs from index -- see `coordinator_core.git.divergence.diverging_paths`)
that unconditional `git add` OVERWRITES the deliberately-staged content with
worktree content, destroying the divergence before any downstream
commit-mechanism selector (`git_native.commit_scoped()`) ever observes it --
reproducing the claude-klabauter 506748a0 incident THROUGH this op.

Uses the real-git fixtures at `fixtures/real_git.py` (this module is
allowlisted in `test_real_git_fixture_boundary.py`'s
`_ALLOWED_REAL_GIT_IMPORTERS`) -- a mocked git has no index, so there is
nothing for a real "staged vs. worktree" divergence to be exhibited against.

Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
§ C10.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from coordinator_core.ops.ceremony.git_native import add_paths

from .fixtures.real_git import make_agree_path, make_diverged_path, real_git_repo

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _staged_blob(root: Path, rel: str) -> str:
    """The CURRENTLY STAGED content of `rel`, read via `git show :rel` --
    never the worktree content."""
    result = _git(["show", f":{rel}"], root)
    return result.stdout


def test_derive_trailers_preserves_diverged_staged_content_verbatim(tmp_path):
    """THE test that would have caught the defect before it shipped: a
    deliberately partial-hunk-staged path survives `_derive_trailers()`
    with its STAGED content untouched -- never silently overwritten by the
    newer worktree content sitting on top of it."""
    root = real_git_repo(tmp_path)
    make_diverged_path(
        root, "docs/notes.md", staged_content="STAGED HUNK\n", worktree_content="LATER EDIT\n"
    )

    trailers = wsc_tail_mod._derive_trailers(
        common_dir=root / ".git",
        sid="11111111-1111-1111-1111-111111111111",
        stage_paths=["docs/notes.md"],
        nature=None,
        explicit_trailers="",
    )

    assert _staged_blob(root, "docs/notes.md") == "STAGED HUNK\n"
    # Worktree content is untouched either way -- this call never mutates
    # the worktree, only (conditionally) the index.
    assert (root / "docs/notes.md").read_text(encoding="utf-8") == "LATER EDIT\n"
    # Best-effort trailer derivation never raises; assert it returned SOME
    # string (possibly empty -- commit.anchors may or may not resolve a
    # Plan: for this non-plan path, irrelevant to this test's assertion).
    assert isinstance(trailers, str)


def test_derive_trailers_red_proof_without_divergence_check_clobbers(tmp_path, monkeypatch):
    """Red-proof for the test above: reproduce the PRE-FIX behaviour by
    making `diverging_paths()` report no divergence (exactly what the old
    code effectively assumed -- it never called `diverging_paths()` at all,
    so every path was always treated as safe to stage) and confirm the
    deliberately-staged hunk IS clobbered. This proves the assertion above
    is a real regression guard, not a tautology."""
    root = real_git_repo(tmp_path)
    make_diverged_path(
        root, "docs/notes.md", staged_content="STAGED HUNK\n", worktree_content="LATER EDIT\n"
    )

    monkeypatch.setattr(wsc_tail_mod, "diverging_paths", lambda paths, cwd=None: [])

    wsc_tail_mod._derive_trailers(
        common_dir=root / ".git",
        sid="11111111-1111-1111-1111-111111111111",
        stage_paths=["docs/notes.md"],
        nature=None,
        explicit_trailers="",
    )

    # Pre-fix behaviour: the deliberately-staged hunk is gone, replaced by
    # worktree content -- exactly the claude-klabauter 506748a0 incident shape.
    assert _staged_blob(root, "docs/notes.md") == "LATER EDIT\n"


def test_derive_trailers_stages_non_diverged_paths_normally(tmp_path):
    """Non-diverged paths (already agreeing, or not yet staged at all) are
    still staged exactly as before -- the fix narrows the skip to genuinely
    diverged paths only, it does not regress the common case."""
    root = real_git_repo(tmp_path)
    make_agree_path(root, "docs/agree.md", "agreed content\n")

    # A second path that is not staged at all yet (plain worktree edit).
    fresh = root / "docs" / "fresh.md"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_text("fresh content\n", encoding="utf-8")

    wsc_tail_mod._derive_trailers(
        common_dir=root / ".git",
        sid="11111111-1111-1111-1111-111111111111",
        stage_paths=["docs/agree.md", "docs/fresh.md"],
        nature=None,
        explicit_trailers="",
    )

    assert _staged_blob(root, "docs/agree.md") == "agreed content\n"
    assert _staged_blob(root, "docs/fresh.md") == "fresh content\n"


def test_derive_trailers_mixed_diverged_and_safe_paths(tmp_path):
    """A realistic mixed batch: one diverged path (preserved verbatim, never
    re-added) alongside one safe path (staged normally) in the SAME call."""
    root = real_git_repo(tmp_path)
    make_diverged_path(
        root, "docs/diverged.md", staged_content="STAGED\n", worktree_content="EDITED\n"
    )
    make_agree_path(root, "docs/safe.md", "safe content\n")

    wsc_tail_mod._derive_trailers(
        common_dir=root / ".git",
        sid="11111111-1111-1111-1111-111111111111",
        stage_paths=["docs/diverged.md", "docs/safe.md"],
        nature=None,
        explicit_trailers="",
    )

    assert _staged_blob(root, "docs/diverged.md") == "STAGED\n"
    assert _staged_blob(root, "docs/safe.md") == "safe content\n"


def test_derive_trailers_explicit_trailers_short_circuits_before_any_staging(tmp_path):
    """Caller-supplied `explicit_trailers` wins verbatim and no staging is
    attempted at all -- a diverged path is left completely untouched."""
    root = real_git_repo(tmp_path)
    make_diverged_path(
        root, "docs/diverged.md", staged_content="STAGED\n", worktree_content="EDITED\n"
    )

    result = wsc_tail_mod._derive_trailers(
        common_dir=root / ".git",
        sid="11111111-1111-1111-1111-111111111111",
        stage_paths=["docs/diverged.md"],
        nature=None,
        explicit_trailers="Nature: chore",
    )

    assert result == "Nature: chore"
    assert _staged_blob(root, "docs/diverged.md") == "STAGED\n"
