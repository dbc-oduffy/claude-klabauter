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
    module._resolve_cli_cmd = lambda name: None

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
