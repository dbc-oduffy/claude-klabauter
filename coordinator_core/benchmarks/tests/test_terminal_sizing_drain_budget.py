"""Pins the terminal-sizing drain benchmark's seeded shape, its measured legs, and the drain interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_sizing_drain_budget as bench
from coordinator_core.ops.fleet import terminal_sizing_drain as drain_mod

pytestmark = pytest.mark.complete


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("sizing-corpus")
    bench.seed_corpus(root)
    return root


def test_seed_shape(corpus: Path) -> None:
    scan = drain_mod.scan_sizings(corpus)
    terminal = scan.terminal()
    forward = [r for r in terminal if r.plan.endswith("live-plan.md")]
    assert len(terminal) == bench.MOVABLE + bench.FORWARD
    assert len(forward) == bench.FORWARD
    assert len(list((corpus / "scratch/warp").iterdir())) == bench.RUNS


def test_scan_reads_head_scalars_and_nulls(tmp_path: Path) -> None:
    sizings = tmp_path / "state/sizings"
    sizings.mkdir(parents=True)
    (sizings / "2026-01-01-a.yaml").write_text(
        "deliverable_id: dlv-a\nroute: roadmap  # c\nxl_exit: null\nstatus: 'shipped'  # c\nplan: null\n", encoding="utf-8"
    )
    (sizings / "2026-01-01-late.yaml").write_text(
        "deliverable_id: dlv-l\nscout:\n" + "  - " + "x" * 5000 + "\nstatus: declined\nplan: docs/plans/p.md\n", encoding="utf-8"
    )
    (sizings / "notes.txt").write_text("status: shipped\n", encoding="utf-8")
    scan = drain_mod.scan_sizings(tmp_path)
    assert sorted(scan.records) == ["state/sizings/2026-01-01-a.yaml", "state/sizings/2026-01-01-late.yaml"]
    first = scan.records["state/sizings/2026-01-01-a.yaml"]
    assert (first.status, first.plan, first.route, first.xl_exit, first.deliverable_id) == ("shipped", "", "roadmap", "", "dlv-a")
    late = scan.records["state/sizings/2026-01-01-late.yaml"]
    assert (late.status, late.plan, late.terminal) == ("declined", "docs/plans/p.md", True)


def test_scan_missing_directory_is_empty(tmp_path: Path) -> None:
    assert drain_mod.scan_sizings(tmp_path).records == {}


def test_baseline_legs_succeed_and_report_figures(tmp_path: Path) -> None:
    rows = bench.run_budget(tmp_path, baseline=True, git_free=True)
    archive, promote, combined = rows
    assert archive["acted"] == bench.MOVABLE and archive["ok"]
    assert promote["promoted"] == 0 and promote["ok"]
    for row in rows:
        assert row["process_ms"] >= 0 and isinstance(row["spawns"], int)
    assert combined["ok"]


def test_default_single_call_leg_succeeds(tmp_path: Path) -> None:
    rows = bench.run_budget(tmp_path, baseline=False, git_free=True)
    archive, promote, combined = rows
    assert archive["acted"] == bench.MOVABLE and archive["ok"]
    assert promote["ok"] and combined["ok"]
