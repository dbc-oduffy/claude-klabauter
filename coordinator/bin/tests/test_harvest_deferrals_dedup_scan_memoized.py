from __future__ import annotations
"""
test_harvest_deferrals_dedup_scan_memoized.py — spawn-count regression test
for coordinator-harvest-deferrals.py's per-row dedup-scan root resolution.

Spec backlink: state/audits/2026-08-15-fleet-composed-op-spawn-census.md row
13 / state/audits/2026-08-15-fleet-census-reverification-at-head.md.

Defect (measured): `_harvest()`'s per-row loop called `_candidate_search_
dirs(row)` once per candidate row, which in turn called `_repo_root()` (a
`git rev-parse --show-toplevel` subprocess spawn), `_resolved_doe_root()`,
and `_resolved_claude_klabauter_root()` (each capable of its own machine-local-
registry/marketplace-cache subprocess spawn) FRESH every time — 3 resolution
calls x N candidate rows for an answer that cannot change within one
process's lifetime.

Fix: memoize all three (`_repo_root_cache` / `_resolved_doe_root_cache` /
`_resolved_claude_klabauter_root_cache`), mirroring the pre-existing `_CLI_CMD_CACHE`
pattern already used for CLI resolution in this same module.

This test is in-process (no subprocess spawn of the real write seams) and
counts calls to the underlying resolution primitives directly — it does NOT
assert on `_harvest()`'s own per-row WRITE dispatch (`_run_queue_append`/
`_run_lesson_promote`), which is deliberately left one-spawn-per-row (see
budget-manifest.json's `bin.coordinator_harvest_deferrals_dedup_scan_root_
resolution` rationale for why that isolation is load-bearing).

Exact-equality assertion, deliberately: a spawn-COUNT budget's whole point is
that it does not drift quietly.
"""

import importlib.util
import json
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin
_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)  # coordinator/
_REPO_ROOT = os.path.dirname(_COORDINATOR_DIR)

_HARVEST_CLI = os.path.join(_BIN_DIR, "coordinator-harvest-deferrals.py")
_MANIFEST_PATH = os.path.join(
    _REPO_ROOT, "coordinator_core", "benchmarks", "budget-manifest.json"
)


def _load_harvest_module():
    if _BIN_DIR not in sys.path:
        sys.path.insert(0, _BIN_DIR)
    loader = SourceFileLoader("coordinator_harvest_deferrals_module_spawn_test", _HARVEST_CLI)
    spec = importlib.util.spec_from_file_location(
        "coordinator_harvest_deferrals_module_spawn_test", _HARVEST_CLI, loader=loader
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_spawn_budget() -> dict:
    with open(_MANIFEST_PATH, encoding="utf-8") as fh:
        manifest = json.load(fh)
    entry = manifest["overrides"]["bin.coordinator_harvest_deferrals_dedup_scan_root_resolution"]
    return entry["spawn_count_budget"]


def test_dedup_scan_root_resolution_memoized_across_candidate_rows() -> None:
    """5 candidate rows must cost exactly the manifest's
    `resolution_calls_for_5_candidate_rows` total SPAWNS across the three
    underlying resolution primitives (`_repo_root`/`doe_root`/`_claude_klabauter_root`),
    not 5x that count."""
    module = _load_harvest_module()

    calls = {"repo_root": 0, "doe_root": 0, "claude_klabauter_root": 0}

    real_subprocess_run = module.subprocess.run

    class _FakeCompleted:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def _fake_subprocess_run(cmd, *a, **kw):
        if cmd[:2] == ["git", "rev-parse"]:
            calls["repo_root"] += 1
            return _FakeCompleted(0, str(Path(_REPO_ROOT)) + "\n")
        return real_subprocess_run(cmd, *a, **kw)

    def _fake_doe_root():
        calls["doe_root"] += 1
        return "/fake/doe-root"

    def _fake_claude_klabauter_root():
        calls["claude_klabauter_root"] += 1
        return "/fake/claude-klabauter-root"

    module.subprocess.run = _fake_subprocess_run
    module.doe_root = _fake_doe_root
    module._claude_klabauter_root = _fake_claude_klabauter_root
    try:
        rows = [{"id": f"D{i}"} for i in range(5)]
        for row in rows:
            module._candidate_search_dirs(row)
    finally:
        module.subprocess.run = real_subprocess_run

    total_calls = calls["repo_root"] + calls["doe_root"] + calls["claude_klabauter_root"]
    budget = _manifest_spawn_budget()
    expected = budget["resolution_calls_for_5_candidate_rows"]

    assert total_calls == expected, (
        f"expected exactly {expected} total resolution calls across 5 "
        f"candidate rows (memoized), got {total_calls}: {calls!r}"
    )
    # `_repo_root()` spawns ZERO times since eacbba04a routed it through
    # `coordinator_core.git.repo_root.show_toplevel`, which walks for the
    # ordinary case and spawns only when the walk finds no `.git` entry — this
    # fixture runs inside a real repo, so the walk always answers. The
    # memoization this test exists to protect is asserted by the two counters
    # below plus the total; a repo_root count above 0 would mean the walk
    # regressed to a spawn, not that memoization broke.
    assert calls["repo_root"] == 0
    assert calls["doe_root"] == 1
    assert calls["claude_klabauter_root"] == 1
