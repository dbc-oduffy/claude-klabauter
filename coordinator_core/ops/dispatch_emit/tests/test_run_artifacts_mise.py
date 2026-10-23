"""run_artifacts.mise_candidate_paths: the inventory family and plan seam sidecars of a mise run."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts as ra

BASE = "20261022T154447-3f2799b8"
INV = "state/mise-inventory"
PLAN_A = "docs/plans/2026-10-22-a.md"
PLAN_B = "docs/plans/2026-10-22-b.md"
PLAN_C = "docs/plans/2026-10-22-c.md"

TABLE = (
    "---\nrun_id: {run}\n---\n\n## Chunk table\n\n"
    "| id | summary | footprint | deps | verification | disposition | spec path |\n"
    "|---|---|---|---|---|---|---|\n"
    "| `A1` | s | f | - | v | open | `{a}` |\n"
    "| `B1` | s | f | - | v | done | {b} |\n"
    "| `A2` | s | f | - | v | open | {a} |\n"
    "| `X` | s | f | - | v | open | - |\n"
)


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    from coordinator_core.git import run as git_run

    def boom(*a, **k):
        raise AssertionError("mise_candidate_paths must not spawn git")

    monkeypatch.setattr(git_run, "run_git", boom)


def _w(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def mise(tmp_path):
    root = tmp_path
    _w(root, f"{INV}/{BASE}.md", TABLE.format(run=BASE, a=PLAN_A, b=PLAN_B))
    for suffix in ("-t1.md", "-t2.md", ".spine.md", "-p1.spine.md", "-t3-p1.spine.md", "-t3.md"):
        _w(root, f"{INV}/{BASE}{suffix}")
    # foreign: shared timestamp prefix, different run
    for foreign in (f"{BASE[:-1]}9.md", f"{BASE[:-1]}9-t1.md", f"{BASE}x.md", f"{BASE}-notes.md"):
        _w(root, f"{INV}/{foreign}")
    for plan in (PLAN_A, PLAN_B, PLAN_C):
        _w(root, plan)
    _w(root, "docs/plans/2026-10-22-a.seam.yaml")
    _w(root, "docs/plans/2026-10-22-a.wave-0.seam.yaml")
    _w(root, "docs/plans/2026-10-22-b.wave-2.seam.yaml")
    _w(root, "docs/plans/2026-10-22-c.seam.yaml")
    return root


def test_inventory_family_only_own(mise):
    got = ra.mise_candidate_paths(mise, BASE).by_class["mise-inventory"]
    assert got == tuple(
        sorted(
            f"{INV}/{BASE}{s}"
            for s in ("-p1.spine.md", "-t1.md", "-t2.md", "-t3-p1.spine.md", "-t3.md", ".md", ".spine.md")
        )
    )


@pytest.mark.parametrize("suffix", ["-t3", "-p1", "-t3-p1"])
def test_family_suffix_run_id_resolves_same_base(mise, suffix):
    assert ra.mise_candidate_paths(mise, BASE + suffix).by_class == ra.mise_candidate_paths(mise, BASE).by_class


def test_seam_sidecars_of_every_chunk_table_plan(mise):
    got = ra.mise_candidate_paths(mise, BASE).by_class["seam-sidecar"]
    assert got == (
        "docs/plans/2026-10-22-a.seam.yaml",
        "docs/plans/2026-10-22-a.wave-0.seam.yaml",
        "docs/plans/2026-10-22-b.wave-2.seam.yaml",
    )


def test_inventory_rel_overrides_table_source(mise):
    _w(mise, "state/other.md", TABLE.format(run=BASE, a=PLAN_C, b=PLAN_C))
    got = ra.mise_candidate_paths(mise, BASE, inventory_rel="state/other.md").by_class["seam-sidecar"]
    assert got == ("docs/plans/2026-10-22-c.seam.yaml",)


def test_missing_inventory_is_empty(tmp_path):
    a = ra.mise_candidate_paths(tmp_path, BASE)
    assert a.paths() == ()


def test_unreadable_table_keeps_family_drops_seams(mise):
    _w(mise, f"{INV}/{BASE}.md", "no table here\n")
    a = ra.mise_candidate_paths(mise, BASE)
    assert "seam-sidecar" not in a.by_class
    assert f"{INV}/{BASE}.md" in a.by_class["mise-inventory"]


@pytest.mark.parametrize("bad", ["", "a/b", "..", "a\\b"])
def test_unsafe_run_id_is_empty(mise, bad):
    assert ra.mise_candidate_paths(mise, bad).paths() == ()


def test_outside_plan_paths_refused(mise):
    _w(mise, f"{INV}/{BASE}.md", TABLE.format(run=BASE, a="../escape/p.md", b="/abs/p.md"))
    assert "seam-sidecar" not in ra.mise_candidate_paths(mise, BASE).by_class


def test_mise_inventory_is_a_record_class():
    assert "mise-inventory" in ra.ARTIFACT_CLASSES
    from coordinator_core.ops.dispatch_emit import terminal_commit

    assert "mise-inventory" in terminal_commit._PRODUCT_ARTIFACT_CLASSES
