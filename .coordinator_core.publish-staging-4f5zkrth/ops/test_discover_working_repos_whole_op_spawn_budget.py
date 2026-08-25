"""coordinator_core/ops/test_discover_working_repos_whole_op_spawn_budget.py

Whole-`main()`-call spawn-count measurement for `discover_working_repos.py`,
closing the gap `test_discover_working_repos_spawn_budget.py`'s
`_spawn_count_rationale` names but does not itself close: `per_call` (1) is
`_tier_a5()` + `_publish_mirror_keys()` alone, a SUB-PATH of the op, not the
op end-to-end. `main()` reaches the sanctioned `_sort_unique` -> `sort -u`
carve-out (`coordinator_core/tests/test_no_bash_dependency.py` names it)
THREE times on a real invocation:

  1. `_tier_a()`'s own `_sort_unique(filtered)[:20]` tail
  2. `_tier_a5()`'s own `_sort_unique(results)` tail
  3. `main()`'s merge tail -- `_sort_unique(_gate_and_dedup(combined, ...))`

...on BOTH the Tier-A-non-empty branch and the Tier-A-empty/Tier-B branch
(there, `_tier_a()` contributes 0 and `_tier_b()`'s own `_sort_unique(
results)[:30]` tail contributes the matching 1). The all-tiers-empty path is
0: every `_sort_unique` call sees an empty list and early-returns before
spawning (see that function's `if not lines: return []` guard).

Why this was previously unmeasured: `_tier_a()` reads `Path.home() /
".claude" / "projects"`, which had no test seam -- calling `main()` in a
test therefore depended on whatever activity records exist on the machine
running the suite. `discover_working_repos.py` now carries
`COORDINATOR_TIER_A_PROJECTS_DIR`, mirroring the existing
`COORDINATOR_TIER_A_FS_ROOT` seam directly above it, closing that gap.

NOTHING is stubbed: every fixture below is a real directory / real `.git`
marker on disk; `subprocess.run` is wrapped only to COUNT real calls it
makes, never to fake one out. `_is_git_root`'s `.git`-presence walk
(`coordinator_core/git/repo_root.py::show_toplevel`) is itself spawn-free
whenever a `.git` entry is found on the walk up from the candidate -- true
for every fixture here by construction -- so the ONLY subprocess this test
observes is the sanctioned `sort -u` carve-out.

Spec: tasks/opro-03-c08-trace/FIX-E-discover-repos-whole-op.md (opro-03 C-08
finding, `state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md`
§ 10). Manifest keys proposed here (`op_total_tier_a_non_empty`,
`op_total_tier_b_fallback`, `op_total_all_empty`) are NOT yet in
`coordinator_core/benchmarks/budget-manifest.json` -- that file is EM-owned
and this test file may not edit it. The manifest-comparison assertions below
are expected to read as a clean, informative failure (measured value vs.
`None`) until the EM lands those keys, NOT a skip.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import budget
from coordinator_core.ops import discover_working_repos as m

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _manifest_spawn_budget() -> dict:
    manifest = budget.load_manifest()
    entry = manifest["overrides"]["ops.discover_working_repos"]
    return entry["spawn_count_budget"]


def _write_repos_registry(reg_dir: Path, repos: dict) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for slug, path in repos.items():
        posix = str(path).replace("\\", "\\\\")
        lines.append(f'"repos.{slug}" = "{posix}"')
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _install_counting_run(monkeypatch) -> dict:
    """Wraps the REAL `subprocess.run` to count calls -- never replaces its
    behavior. Installed only around the measured `main()` call, never
    during fixture setup, so setup I/O is never mistaken for the op's own
    spawns."""
    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)
    return call_count


class TestWholeOpSpawnBudget:
    """Three named shapes -- Tier-A-non-empty, Tier-A-empty/Tier-B, and
    all-tiers-empty -- are genuinely different codepaths through `main()`,
    not one shape parameterized three ways."""

    def test_tier_a_non_empty_branch_spawns_three(self, tmp_path, monkeypatch) -> None:
        """A + A.5 + merge. Tier A fires via the fast-path (non-hyphenated)
        decode, so this does not also exercise the greedy-decode fallback
        (covered separately in `test_discover_working_repos.py`). The Tier
        A entry itself need not exist on the REAL filesystem -- only inside
        `COORDINATOR_TIER_A_FS_ROOT`'s fixture tree, per that seam's own
        contract -- `_gate_and_dedup` drops a nonexistent candidate with
        ZERO spawns (`_is_git_root`'s `os.path.isdir` guard returns False
        before any walk). The item that must survive the gate -- so
        `main()`'s OWN merge tail has something non-empty to sort, and
        therefore spawns -- comes through Tier A.5 (registry) instead: a
        real directory with a real `.git` marker, so `show_toplevel`'s walk
        finds it with zero spawns of its own.
        """
        marker = "opro03c08fixturemarkernonexistentaonly"
        projects_dir = tmp_path / "projects"
        (projects_dir / f"X--{marker}").mkdir(parents=True)
        fs_root = tmp_path / "fsroot"
        (fs_root / marker).mkdir(parents=True)
        monkeypatch.setenv("COORDINATOR_TIER_A_PROJECTS_DIR", str(projects_dir))
        monkeypatch.setenv("COORDINATOR_TIER_A_FS_ROOT", str(fs_root))

        real_repo = tmp_path / "a5repo"
        (real_repo / ".git").mkdir(parents=True)
        reg_dir = tmp_path / "registry"
        _write_repos_registry(reg_dir, {"realrepo": real_repo})
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

        # Sanity check, run BEFORE the counting wrapper is installed (its own
        # `_sort_unique` spawn must not pollute the measured count below):
        # confirms Tier A actually fired via the fast path, not a fallback.
        assert m._tier_a() == [f"X:\\{marker}"]  # abs-path-ok: synthetic nonexistent fixture marker, not a real machine path

        call_count = _install_counting_run(monkeypatch)
        rc = m.main([])
        assert rc == 0
        assert call_count["n"] == 3, (
            "measured whole-op subprocess.run count, Tier-A-non-empty "
            f"branch: {call_count['n']} (expected 3: _tier_a's tail, "
            "_tier_a5's tail, main()'s own merge tail)"
        )

        budget_entry = _manifest_spawn_budget()
        assert budget_entry.get("op_total_tier_a_non_empty") == call_count["n"], (
            "manifest key spawn_count_budget.op_total_tier_a_non_empty is "
            f"missing or stale -- measured value is {call_count['n']}"
        )

    def test_tier_a_empty_tier_b_branch_spawns_three(self, tmp_path, monkeypatch) -> None:
        """B + A.5 + merge. Tier A is empty by construction here: this test
        does NOT set `COORDINATOR_TIER_A_PROJECTS_DIR`, so `_tier_a()` falls
        through to `Path.home() / ".claude" / "projects"`, which the
        suite-root `_quarantine_real_home` autouse fixture has already
        pointed at a fresh, empty per-test directory -- confirmed below,
        not assumed."""
        assert m._tier_a() == []

        b_repo = tmp_path / "tierb-dev"
        b_repo.mkdir()
        (b_repo / ".git").mkdir()
        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(b_repo)])

        real_repo = tmp_path / "a5repo"
        (real_repo / ".git").mkdir(parents=True)
        reg_dir = tmp_path / "registry"
        _write_repos_registry(reg_dir, {"realrepo": real_repo})
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

        call_count = _install_counting_run(monkeypatch)
        rc = m.main([])
        assert rc == 0
        assert call_count["n"] == 3, (
            "measured whole-op subprocess.run count, Tier-A-empty/Tier-B "
            f"branch: {call_count['n']} (expected 3: _tier_b's tail, "
            "_tier_a5's tail, main()'s own merge tail)"
        )

        budget_entry = _manifest_spawn_budget()
        assert budget_entry.get("op_total_tier_b_fallback") == call_count["n"], (
            "manifest key spawn_count_budget.op_total_tier_b_fallback is "
            f"missing or stale -- measured value is {call_count['n']}"
        )

    def test_all_tiers_empty_spawns_zero(self, tmp_path, monkeypatch) -> None:
        """A, A.5, and B all empty -- every `_sort_unique` call this shape
        reaches sees an empty list and early-returns before spawning."""
        assert m._tier_a() == []

        monkeypatch.setattr(m, "_TIER_B_CANDIDATES", [str(tmp_path / "does-not-exist")])
        monkeypatch.setenv(
            "MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "no-such-registry-dir")
        )

        call_count = _install_counting_run(monkeypatch)
        rc = m.main([])
        assert rc == 0
        assert call_count["n"] == 0, (
            "measured whole-op subprocess.run count, all-tiers-empty "
            f"branch: {call_count['n']} (expected 0)"
        )

        budget_entry = _manifest_spawn_budget()
        assert budget_entry.get("op_total_all_empty") == call_count["n"], (
            "manifest key spawn_count_budget.op_total_all_empty is missing "
            f"or stale -- measured value is {call_count['n']}"
        )

    def test_existing_keys_still_present(self) -> None:
        """Negative-spec pin: this file adds op-total keys, it does not
        replace or rename the existing sub-path keys -- see this module's
        docstring and the brief's hard constraint against renaming them."""
        budget_entry = _manifest_spawn_budget()
        assert budget_entry["per_call"] == 1
        assert budget_entry["machine_local_cli_elimination_calls"] == 0
