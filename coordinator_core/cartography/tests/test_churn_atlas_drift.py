"""
coordinator_core.cartography.tests.test_churn_atlas_drift

Tests for the C2/C3 additive atlas-comparison fields on the "cartography.churn"
op (coordinator_core.cartography.atlas_record + .churn.compare_against_recorded_
atlas), per docs/plans/2026-08-06-churn-emergent-detection-file-granularity.md.

This is the headline-AC test module: a new file added beneath an
already-catalogued directory must surface in `drifted_systems`, never in a
redefined `emergent` and never in `uncatalogued` — the whole point of the
Decision-ruled three-way split is that each case has exactly one home.

Fixtures are synthetic (`tmp_path` + a real `git init`), constructing a
minimal `docs/architecture/file-index.md` + `docs/architecture/systems/*.md`
pair per case — never by re-running the architecture survey or hand-editing
this repo's own recorded atlas (plan Anti-scope).

Spec backlink: pln-cartography-churn-emergent-det-8f59ce
§ chunk C4 (AC1, AC1b, AC2/AC5 denominator, AC7, AC8).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import coordinator_core.ops.cartography_churn  # noqa: F401 — fires @register_op
from coordinator_core.ops.cartography_churn import _cartography_churn


def _run(coro):
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=True,
    )


def _make_git_repo(tmp_path: Path) -> Path:
    """Mirrors test_churn._make_git_repo's convention."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "churn-drift-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Churn Drift Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


_FILE_INDEX_TEMPLATE = """\
---
last_mapped: {last_mapped}
---

# File index (synthetic fixture)

## Directory → system

### cartography — recorded package

`coordinator_core/cartography/` is the recorded package for this fixture.

### stable — recorded package

`coordinator_core/stable/` is a second recorded package, used to prove an
already-accounted-for file stays out of both `uncatalogued` and
`drifted_systems`.

### The remaining systems

| System | Directories | Files | Lines |
|---|---|---|---|
"""


def _write_recorded_atlas(
    repo: Path,
    *,
    last_mapped: str = "2026-08-06",
    cartography_files: int = 8,
    stable_files: int = 2,
) -> None:
    """Write a minimal synthetic docs/architecture/{file-index.md,systems/*.md}
    pair — the supported way to control recorded state (plan Anti-scope
    forbids hand-editing the real, generated file-index.md)."""
    arch_dir = repo / "docs" / "architecture"
    arch_dir.mkdir(parents=True, exist_ok=True)
    (arch_dir / "file-index.md").write_text(
        _FILE_INDEX_TEMPLATE.format(last_mapped=last_mapped), encoding="utf-8"
    )

    systems_dir = arch_dir / "systems"
    systems_dir.mkdir(parents=True, exist_ok=True)
    (systems_dir / "cartography.md").write_text(
        f"---\nsystem: cartography\nfiles: {cartography_files}\n---\n\n"
        "# cartography (synthetic fixture)\n",
        encoding="utf-8",
    )
    (systems_dir / "stable.md").write_text(
        f"---\nsystem: stable\nfiles: {stable_files}\n---\n\n"
        "# stable (synthetic fixture)\n",
        encoding="utf-8",
    )


def _write_py_files(repo: Path, relpaths: list[str]) -> None:
    for relpath in relpaths:
        path = repo / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")


def _call_churn(repo: Path, system_dirs: list[str] | None = None) -> dict:
    return _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": "1970-01-01",
                "system_dirs": system_dirs or ["nonexistent_system_dir"],
                "excluded_dirs": [],
            },
            repo_root=repo,
        )
    )


# ---------------------------------------------------------------------------
# AC1 (headline) — new file under an already-catalogued directory
# ---------------------------------------------------------------------------


def test_new_file_under_catalogued_directory_surfaces_in_drifted_systems(tmp_path):
    """AC1: coordinator_core/cartography/ is recorded with files: 8. Eight
    tracked files satisfy that fingerprint; a ninth (new) file beneath the
    same directory pushes live membership to 9. The system must surface in
    `drifted_systems` with live_files > recorded_files and delta > 0 — and
    must NOT appear in `emergent` (unchanged, diff-window semantics) or in
    `uncatalogued` (the recorded mapping still covers this file)."""
    repo = _make_git_repo(tmp_path)
    _write_recorded_atlas(repo, cartography_files=8, stable_files=2)

    cartography_files = [
        f"coordinator_core/cartography/f{i}.py" for i in range(1, 9)
    ]
    stable_files = [f"coordinator_core/stable/g{i}.py" for i in range(1, 3)]
    _write_py_files(repo, cartography_files + stable_files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed recorded 8 cartography files + 2 stable files")

    # The new file — beneath the already-catalogued directory, not accounted
    # for by the recorded files: 8 fingerprint.
    _write_py_files(repo, ["coordinator_core/cartography/new_module.py"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new_module.py beneath cartography/")

    # system_dirs scopes cartography/ as catalogued for the pre-existing
    # diff-window `emergent` path — git pathspecs are recursive, so
    # new_module.py is invisible to `emergent` under the OLD mechanism too.
    # This is exactly the blindness the plan describes: drifted_systems is
    # the field that catches it instead.
    result = _call_churn(
        repo, system_dirs=["coordinator_core/cartography", "coordinator_core/stable"]
    )

    assert "atlas_unreadable" not in result

    drifted_by_system = {d["system"]: d for d in result["drifted_systems"]}
    assert "cartography" in drifted_by_system
    drift = drifted_by_system["cartography"]
    assert drift["recorded_files"] == 8
    assert drift["live_files"] == 9
    assert drift["delta"] == 1
    assert drift["live_files"] > drift["recorded_files"]

    assert "coordinator_core/cartography/new_module.py" not in result["emergent"]
    assert "coordinator_core/cartography/new_module.py" not in result["uncatalogued"]


# ---------------------------------------------------------------------------
# AC1b — package absent from the recorded table lands in uncatalogued
# ---------------------------------------------------------------------------


def test_package_absent_from_recorded_table_lands_in_uncatalogued(tmp_path):
    """AC1b: coordinator_core/widgets/ is not in the recorded package_systems
    table (no rule covers it) — a source file beneath it must land in
    `uncatalogued`, not `drifted_systems` (no recorded system to attach it
    to) and not `emergent` (unchanged diff-window semantics)."""
    repo = _make_git_repo(tmp_path)
    _write_recorded_atlas(repo, cartography_files=8, stable_files=2)

    _write_py_files(
        repo,
        [f"coordinator_core/cartography/f{i}.py" for i in range(1, 9)]
        + [f"coordinator_core/stable/g{i}.py" for i in range(1, 3)]
        + ["coordinator_core/widgets/unmapped.py"],
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed recorded files + an unmapped package")

    result = _call_churn(repo)

    assert "atlas_unreadable" not in result
    assert "coordinator_core/widgets/unmapped.py" in result["uncatalogued"]
    drifted_systems = {d["system"] for d in result["drifted_systems"]}
    assert "widgets" not in drifted_systems


# ---------------------------------------------------------------------------
# Neither — a file already accounted for is in neither field
# ---------------------------------------------------------------------------


def test_already_accounted_for_file_is_in_neither_field(tmp_path):
    """A recorded system whose live membership exactly matches its recorded
    fingerprint (delta == 0) is excluded from `drifted_systems` entirely, and
    its files — covered by the recorded mapping — never appear in
    `uncatalogued`. This is the "exactly one home" property's negative case."""
    repo = _make_git_repo(tmp_path)
    _write_recorded_atlas(repo, cartography_files=8, stable_files=2)

    stable_files = [f"coordinator_core/stable/g{i}.py" for i in range(1, 3)]
    _write_py_files(
        repo,
        [f"coordinator_core/cartography/f{i}.py" for i in range(1, 9)] + stable_files,
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed recorded 8 cartography + 2 stable, no drift")

    result = _call_churn(repo)

    assert "atlas_unreadable" not in result
    drifted_systems = {d["system"] for d in result["drifted_systems"]}
    assert "stable" not in drifted_systems
    for relpath in stable_files:
        assert relpath not in result["uncatalogued"]


# ---------------------------------------------------------------------------
# AC7 — pre-existing fields are unperturbed by the atlas comparison
# ---------------------------------------------------------------------------


def test_pre_existing_fields_unchanged_by_atlas_comparison(tmp_path):
    """AC7: emergent, excluded_by_prefilter, deleted_at_head, churn_ratio, and
    catalogued_count keep their pre-existing values against the same fixture
    that exercises AC1 — the additive atlas fields must not perturb them."""
    repo = _make_git_repo(tmp_path)
    _write_recorded_atlas(repo, cartography_files=8, stable_files=2)

    _write_py_files(
        repo,
        [f"coordinator_core/cartography/f{i}.py" for i in range(1, 9)]
        + [f"coordinator_core/stable/g{i}.py" for i in range(1, 3)],
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed recorded files")

    _write_py_files(repo, ["coordinator_core/cartography/new_module.py"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add new_module.py beneath cartography/")

    result = _call_churn(repo)

    # system_dirs=["nonexistent_system_dir"] means nothing is "catalogued"
    # for the diff-window path, so every churned-and-still-present path is
    # emergent under the pre-existing semantics.
    assert set(result["emergent"]) == {
        f"coordinator_core/cartography/f{i}.py" for i in range(1, 9)
    } | {f"coordinator_core/stable/g{i}.py" for i in range(1, 3)} | {
        "coordinator_core/cartography/new_module.py",
        "docs/architecture/file-index.md",
        "docs/architecture/systems/cartography.md",
        "docs/architecture/systems/stable.md",
    }
    assert result["excluded_by_prefilter"] == []
    assert result["deleted_at_head"] == []
    assert result["churn_ratio"] == 0.0
    assert result["catalogued_count"] == 0


# ---------------------------------------------------------------------------
# AC5 — catalogued_source_count is the stated denominator
# ---------------------------------------------------------------------------


def test_catalogued_source_count_is_the_considered_candidate_population(tmp_path):
    """AC5: catalogued_source_count equals the candidate population the
    comparison was drawn from (RecordedExpansion.considered_count) — the
    count of tracked .py/.js source candidates surviving the recorded input
    filter, not merely len(uncatalogued) or a per-system count."""
    repo = _make_git_repo(tmp_path)
    _write_recorded_atlas(repo, cartography_files=8, stable_files=2)

    cartography_files = [f"coordinator_core/cartography/f{i}.py" for i in range(1, 9)]
    stable_files = [f"coordinator_core/stable/g{i}.py" for i in range(1, 3)]
    unmapped = ["coordinator_core/widgets/unmapped.py"]
    # A test file, which the recorded input filter excludes outright — must
    # NOT count toward the denominator.
    excluded_test_file = ["coordinator_core/cartography/test_something.py"]

    _write_py_files(repo, cartography_files + stable_files + unmapped + excluded_test_file)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed files including an unmapped pkg and a test file")

    result = _call_churn(repo)

    assert "atlas_unreadable" not in result
    expected_denominator = len(cartography_files) + len(stable_files) + len(unmapped)
    assert result["catalogued_source_count"] == expected_denominator
    # Distinguishable from a smaller population (e.g. len(uncatalogued)).
    assert result["catalogued_source_count"] != len(result["uncatalogued"])


# ---------------------------------------------------------------------------
# AC8 — unreadable atlas is an explicit discriminated failure
# ---------------------------------------------------------------------------


def test_missing_docs_architecture_yields_atlas_unreadable(tmp_path):
    """AC8: a fixture repo with no docs/architecture/ at all returns
    atlas_unreadable with a non-empty reason, empty uncatalogued/
    drifted_systems, last_mapped is None, catalogued_source_count == 0 — and
    the five pre-existing fields are still present and well-formed. No
    exception, no top-level `error` key."""
    repo = _make_git_repo(tmp_path)

    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("z = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add src/real.py, no docs/architecture/ at all")

    result = _call_churn(repo)

    assert "error" not in result
    assert "atlas_unreadable" in result
    assert result["atlas_unreadable"]["reason"]
    assert result["uncatalogued"] == []
    assert result["drifted_systems"] == []
    assert result["last_mapped"] is None
    assert result["catalogued_source_count"] == 0

    for field in ("emergent", "excluded_by_prefilter", "deleted_at_head"):
        assert isinstance(result[field], list)
    assert isinstance(result["churn_ratio"], float)
    assert isinstance(result["catalogued_count"], int)
