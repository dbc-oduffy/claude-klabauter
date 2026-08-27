"""
coordinator_core.distill.tests.test_sidecar_sweep

Unit tests for coordinator_core.distill.sidecar_sweep.

Coverage:
  find_sidecar_candidates:
    (a) finds a seeded sidecar fixture (full-suffix-anchored) under a scan root
        (archive/specs has zero real sidecars post-1084fad8, so this test
        seeds its own fixture rather than relying on repo state)
    (b) finds a timestamped-variant sidecar
    (c) does NOT match a bare-substring / non-anchored file
    (d) returns [] for a non-existent scan root
    (e) recursive: finds sidecars nested in subdirectories
  sweep_sidecars:
    (f) a sidecar with no active reference anywhere is emitted as a
        deletion-manifest row
    (g) a sidecar that IS actively referenced under docs/ is NOT swept —
        it appears in `retained`, not `deletion_manifest`
    (h) ordering invariant: the deletion_manifest never contains a path that
        the active-reference guard would flag as referenced (cross-checked
        directly against active_reference_guard)
    (i) a non-sidecar file alongside sidecars is ignored entirely

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C2
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from coordinator_core.distill._common import active_reference_guard
from coordinator_core.distill.sidecar_sweep import (
    find_sidecar_candidates,
    sweep_sidecars,
)

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")


# ---------------------------------------------------------------------------
# find_sidecar_candidates
# ---------------------------------------------------------------------------


def test_finds_seeded_sidecar_fixture(tmp_path):
    scan_root = tmp_path / "tasks" / "some-workstream"
    scan_root.mkdir(parents=True)
    sidecar = scan_root / "2026-07-12-some-plan.review.md"
    sidecar.write_text("scaffolding content\n")

    found = find_sidecar_candidates(scan_root)

    assert sidecar in found


def test_finds_timestamped_variant(tmp_path):
    scan_root = tmp_path / "tasks"
    scan_root.mkdir()
    sidecar = scan_root / "2026-07-12_143022-slug.c0-findings.md"
    sidecar.write_text("findings\n")

    found = find_sidecar_candidates(scan_root)

    assert sidecar in found


def test_rejects_bare_substring_hit(tmp_path):
    scan_root = tmp_path / "tasks"
    scan_root.mkdir()
    not_a_sidecar = scan_root / "my-review-notes.md"
    not_a_sidecar.write_text("not scaffolding\n")

    found = find_sidecar_candidates(scan_root)

    assert not_a_sidecar not in found
    assert found == []


def test_nonexistent_scan_root_returns_empty(tmp_path):
    assert find_sidecar_candidates(tmp_path / "does-not-exist") == []


def test_recursive_nested_sidecar_found(tmp_path):
    nested = tmp_path / "tasks" / "workstream-a" / "sub"
    nested.mkdir(parents=True)
    sidecar = nested / "foo.plan-coverage-check.md"
    sidecar.write_text("coverage check\n")

    found = find_sidecar_candidates(tmp_path)

    assert sidecar in found


# ---------------------------------------------------------------------------
# sweep_sidecars
# ---------------------------------------------------------------------------


@_requires_rg
def test_unreferenced_sidecar_is_swept(tmp_path):
    repo_root = tmp_path
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "unrelated.md").write_text("nothing relevant here\n")

    scan_root = repo_root / "tasks"
    scan_root.mkdir()
    sidecar = scan_root / "2026-07-12-orphan-plan.review.md"
    sidecar.write_text("orphaned scaffolding\n")

    result = sweep_sidecars(scan_root, repo_root)

    swept_paths = [row["path"] for row in result.deletion_manifest]
    assert "tasks/2026-07-12-orphan-plan.review.md" in swept_paths
    assert result.retained == []


@_requires_rg
def test_actively_referenced_sidecar_is_not_swept(tmp_path):
    repo_root = tmp_path
    docs = repo_root / "docs"
    docs.mkdir()

    scan_root = repo_root / "tasks"
    scan_root.mkdir()
    sidecar = scan_root / "2026-07-12-active-plan.review.md"
    sidecar.write_text("still relevant scaffolding\n")

    rel_path = "tasks/2026-07-12-active-plan.review.md"
    (docs / "some-doc.md").write_text(f"see {rel_path} for details\n")

    result = sweep_sidecars(scan_root, repo_root)

    swept_paths = [row["path"] for row in result.deletion_manifest]
    retained_paths = [row["path"] for row in result.retained]

    assert rel_path not in swept_paths
    assert rel_path in retained_paths
    assert result.retained[0]["reason"] == "active-reference"


@_requires_rg
def test_deletion_manifest_never_contains_actively_referenced_path(tmp_path):
    repo_root = tmp_path
    docs = repo_root / "docs"
    docs.mkdir()

    scan_root = repo_root / "tasks"
    scan_root.mkdir()
    referenced = scan_root / "2026-07-12-referenced.review.md"
    referenced.write_text("x\n")
    unreferenced = scan_root / "2026-07-12-unreferenced.review.md"
    unreferenced.write_text("y\n")

    (docs / "some-doc.md").write_text("tasks/2026-07-12-referenced.review.md is cited here\n")

    result = sweep_sidecars(scan_root, repo_root)

    for row in result.deletion_manifest:
        assert active_reference_guard(row["path"], repo_root) is False


@_requires_rg
def test_process_count_does_not_grow_with_the_set(tmp_path, monkeypatch):
    """The amplification property: sweeping N sidecar candidates must cost the same
    `rg` process count as sweeping one — `sweep_sidecars` batches the active-reference
    guard lookup into a single `rg -f <patternfile>` call regardless of N."""
    repo_root = tmp_path
    (repo_root / "docs").mkdir()
    (repo_root / "docs" / "unrelated.md").write_text("nothing relevant here\n")

    scan_root = repo_root / "tasks"
    scan_root.mkdir()

    spawns: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        spawns.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    (scan_root / "2026-07-12-one.review.md").write_text("a\n")
    sweep_sidecars(scan_root, repo_root)
    after_one = len(spawns)

    for n in range(2, 6):
        (scan_root / f"2026-07-12-item-{n}.review.md").write_text("a\n")
    sweep_sidecars(scan_root, repo_root)
    after_many = len(spawns)

    assert after_many - after_one == after_one, (
        f"5 candidates cost {after_many - after_one} rg spawns against {after_one} for 1: "
        f"{spawns[after_one:]}"
    )


@_requires_rg
def test_non_sidecar_file_ignored(tmp_path):
    repo_root = tmp_path
    (repo_root / "docs").mkdir()

    scan_root = repo_root / "tasks"
    scan_root.mkdir()
    (scan_root / "plain-design-doc.md").write_text("not scaffolding\n")

    result = sweep_sidecars(scan_root, repo_root)

    assert result.deletion_manifest == []
    assert result.retained == []
