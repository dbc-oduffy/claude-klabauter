"""test_harvest_deferrals_evidence_scan_hoisted.py — spawn/IO-count
regression test for Defect 2 of the 2026-08-15 staff review of
coordinator-harvest-deferrals.py.

Spec backlink: coordinator_core/benchmarks/budget-manifest.json's
`bin.coordinator_harvest_deferrals_dedup_scan_root_resolution` override
(`directory_scan_calls_for_5_candidate_rows`).

Defect (measured): `_harvest()`'s per-row loop called `_already_harvested`
once per candidate row, which re-globbed AND re-read EVERY `*.yaml` file in
up to four directories (state/improvement-queue, state/lessons-outbox, in
both claude-klabauter and DoE) on every call — this module's own
`_derive_proposed_action` docstring measures those corpora at 605/493
entries, i.e. O(rows x ~1100 full file reads) for a key set invariant
across the whole loop. An earlier pass memoized the CHEAP root-resolution
half of this per-row work (see test_harvest_deferrals_dedup_scan_memoized.py)
and left this, the expensive half, unhoisted.

Fix: `_collect_evidence_lines(search_dirs)` does the glob+read pass ONCE,
before the loop; `_already_harvested(key, evidence_lines)` is then an
O(1)-per-row substring check against the pre-collected list.

This test is in-process — it counts calls to `glob.glob` (the I/O-shaped
primitive both the old and new code paths funnel through) rather than
timing wall-clock, per the dispatch brief's "never assert wall-clock"
constraint.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys


def _script_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "coordinator-harvest-deferrals.py",
    )


def _manifest_path() -> str:
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    coordinator_dir = os.path.dirname(bin_dir)
    repo_root = os.path.dirname(coordinator_dir)
    return os.path.join(repo_root, "coordinator_core", "benchmarks", "budget-manifest.json")


def _load_harvest_module():
    path = _script_path()
    loader = importlib.machinery.SourceFileLoader(
        "_test_harvest_deferrals_evidence_scan_hoisted", path
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def _manifest_spawn_budget() -> dict:
    with open(_manifest_path(), encoding="utf-8") as fh:
        manifest = json.load(fh)
    entry = manifest["overrides"]["bin.coordinator_harvest_deferrals_dedup_scan_root_resolution"]
    return entry["spawn_count_budget"]


def test_evidence_directory_scan_runs_once_per_harvest_not_per_row(tmp_path):
    """5 candidate rows must cost exactly the manifest's
    `directory_scan_calls_for_5_candidate_rows` total calls to `glob.glob`
    across the whole `_harvest()` invocation, not 5x that count."""
    module = _load_harvest_module()

    scan_dir = tmp_path / "improvement-queue"
    scan_dir.mkdir()
    (scan_dir / "entry-1.yaml").write_text(
        "id: pre-existing\nevidence: harvest-key: some-other-plan:X1\n", encoding="utf-8"
    )

    glob_calls = {"n": 0}
    real_glob = module.glob.glob

    def _counting_glob(*a, **kw):
        glob_calls["n"] += 1
        return real_glob(*a, **kw)

    module.glob.glob = _counting_glob
    module._candidate_search_dirs = lambda row: [str(scan_dir)]
    module._resolve_cli_cmd = lambda name: None  # never actually dispatch a write

    try:
        candidates = [
            {"id": f"D{i}", "title": f"Row {i}", "change_kind": "code-edit", "surface": "x.py", "body": f"Row {i} body."}
            for i in range(5)
        ]
        module._harvest("pln-test-plan-abc123", candidates, dry_run=True)
    finally:
        module.glob.glob = real_glob

    budget = _manifest_spawn_budget()
    expected = budget["directory_scan_calls_for_5_candidate_rows"]

    assert glob_calls["n"] == expected, (
        f"expected exactly {expected} glob.glob call(s) across 5 candidate rows "
        f"(evidence scan hoisted out of the loop), got {glob_calls['n']}"
    )
