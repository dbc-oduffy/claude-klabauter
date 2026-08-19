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
counts REAL `subprocess.run` invocations reachable from `_repo_root()` /
`_resolved_doe_root()` / `_resolved_claude_klabauter_root()` — it does NOT assert on
`_harvest()`'s own per-row WRITE dispatch (`_run_queue_append`/
`_run_lesson_promote`), which is deliberately left one-spawn-per-row (see
budget-manifest.json's `bin.coordinator_harvest_deferrals_dedup_scan_root_
resolution` rationale for why that isolation is load-bearing).

HONEST-COUNTER FIX (opro-03 C-08, `state/audits/2026-08-19-opro-03-c08-
budgeted-op-spawn-trace.md` § 5): a prior version of this test substituted
`doe_root`/`_claude_klabauter_root` with fakes and counted CALLS TO THE FAKES, so
`resolution_calls_for_5_candidate_rows` measured call SHAPE (how many times
`_candidate_search_dirs` reaches each resolver, post-memoization), never a
real spawn — the fakes' own bodies, which is where any subprocess would
actually happen, never ran. This version calls the REAL `doe_root()` /
`_claude_klabauter_root()` (no substitution) with every resolver env override
(`REPO_DOE_CLAUDE`/`DOE_ROOT`/`CLAUDE_KLABAUTER_ROOT`/`QUEUE_APPEND_OUTPUT_ROOT`/
`LESSON_PROMOTE_OUTBOX_ROOT`) explicitly cleared — the steady state on an
installed machine where none of those overrides is set — and counts real
`subprocess.run` calls via a global patch on the `subprocess` module object
(catches every module's `subprocess.run(...)`, not just one function
reference). Clearing the env overrides is required for hermeticity: with
them ambiently set (as this repo's own dev environment has
`REPO_DOE_CLAUDE` set), `doe_root()`'s rung 1b would short-circuit before
ever reaching its `machine-local get repos.doe_claude` spawn, undercounting
the steady-state figure this budget exists to protect.

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


_ENV_OVERRIDES_TO_CLEAR = (
    "REPO_DOE_CLAUDE",
    "DOE_ROOT",
    "CLAUDE_KLABAUTER_ROOT",
    "QUEUE_APPEND_OUTPUT_ROOT",
    "LESSON_PROMOTE_OUTBOX_ROOT",
)


def test_dedup_scan_root_resolution_memoized_across_candidate_rows(monkeypatch) -> None:
    """5 candidate rows must cost exactly the manifest's
    `resolution_calls_for_5_candidate_rows` total REAL `subprocess.run`
    SPAWNS reachable from the three underlying resolution primitives
    (`_repo_root`/`doe_root`/`_claude_klabauter_root`), not 5x that count.

    Calls the REAL `doe_root()`/`_claude_klabauter_root()` (no substitution) with
    every resolver env override cleared — the steady state on an installed
    machine (see module docstring's HONEST-COUNTER FIX). This machine's own
    dev environment has `REPO_DOE_CLAUDE` set ambiently, which would
    short-circuit `doe_root()` before its spawning rung; clearing it here is
    what makes the count observe the real spawn instead of skipping it.
    """
    module = _load_harvest_module()

    for var in _ENV_OVERRIDES_TO_CLEAR:
        monkeypatch.delenv(var, raising=False)

    call_count = {"n": 0, "cmds": []}
    real_subprocess_run = module.subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        cmd = args[0] if args else kwargs.get("args")
        call_count["cmds"].append(cmd)
        return real_subprocess_run(*args, **kwargs)

    module.subprocess.run = _counting_run
    try:
        rows = [{"id": f"D{i}"} for i in range(5)]
        for row in rows:
            module._candidate_search_dirs(row)
    finally:
        module.subprocess.run = real_subprocess_run

    total_calls = call_count["n"]
    budget = _manifest_spawn_budget()
    expected = budget["resolution_calls_for_5_candidate_rows"]

    assert total_calls == expected, (
        f"expected exactly {expected} total REAL subprocess.run calls across 5 "
        f"candidate rows (memoized), got {total_calls}: {call_count['cmds']!r}"
    )
    # `_repo_root()` spawns ZERO times since eacbba04a routed it through
    # `coordinator_core.git.repo_root.show_toplevel`, which walks for the
    # ordinary case and spawns only when the walk finds no `.git` entry — this
    # fixture runs inside a real repo, so the walk always answers. Measured
    # live (opro-03 C-08): the real `doe_root()`/`_claude_klabauter_root()` each make
    # exactly one `subprocess.run` call (a `machine-local get <key>` spawn,
    # memoized after the first row) — no `git rev-parse` invocation appears
    # in `call_count["cmds"]` at all, confirming the walk answered without
    # falling back to a spawn.
    assert not any(cmd[:2] == ["git", "rev-parse"] for cmd in call_count["cmds"] if cmd)
