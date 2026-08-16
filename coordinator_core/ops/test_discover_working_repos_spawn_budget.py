"""coordinator_core/ops/test_discover_working_repos_spawn_budget.py

Spawn-count regression for `discover_working_repos.py::_tier_a5` and
`::_publish_mirror_keys`, following the exact-equality `spawn_count_budget`
template `coordinator_core/benchmarks/budget-manifest.json`'s
`overrides["bin.freeze_review_diff.paths_contributing_nothing"]` entry
already carries (see `coordinator/tests/test_freeze_review_diff_spawn_budget.py`
for the sibling instance of this template).

WHY A SPAWN COUNT, NOT A LATENCY FIGURE: this repo runs 50-70 concurrent LLM
sessions at any given moment (CLAUDE.md's "Load norm" section) — a wall-clock
assertion would be noise. Spawn count is deterministic under load and only
moves when the code changes how many subprocesses it issues per call shape.

Both functions used to spawn one `machine-local keys` subprocess plus one
`machine-local get <key>` subprocess per matched `repos.*` /
`publish.mirrors.*.path` key — measured live at ~24 interpreter spawns per
`discover_working_repos.main()` call (19 `repos.*` keys + 4 mirror paths),
against `machine_resolver.py`'s explicit negative-spec ("Do NOT shell out to
a `machine-local` CLI/binary", 2026-08-05 PM directive "De-bash
spawn-amplification hardening"). Both now read the registry directly via
`coordinator_core.machine_resolver.load_flat_registry_file`/`registry_dir`
(`discover_working_repos.py::_merged_flat_registry`) — zero subprocesses,
any key count.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.benchmarks import budget
from coordinator_core.ops import discover_working_repos as m

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _manifest_spawn_budget() -> dict:
    manifest = budget.load_manifest()
    entry = manifest["overrides"]["ops.discover_working_repos"]
    return entry["spawn_count_budget"]


def _write_registry(reg_dir, repos: dict, mirrors: dict) -> None:
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for slug, path in repos.items():
        posix = str(path).replace("\\", "\\\\")
        lines.append(f'"repos.{slug}" = "{posix}"')
    for slug, path in mirrors.items():
        posix = str(path).replace("\\", "\\\\")
        lines.append(f'"publish.mirrors.{slug}.path" = "{posix}"')
    (reg_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tier_a5_and_publish_mirror_keys_spawn_zero_subprocesses(
    tmp_path, monkeypatch
) -> None:
    """Many `repos.*`/`publish.mirrors.*.path` keys must still cost exactly
    the manifest's `spawn_count_budget.per_call` `subprocess.run`
    invocations — zero, regardless of key count (was O(N) `machine-local`
    spawns before this fix)."""
    budget_entry = _manifest_spawn_budget()
    assert budget_entry == {"per_call": 0}

    reg_dir = tmp_path / "registry"
    repo_paths = {}
    for i in range(5):
        repo_dir = tmp_path / f"repo{i}"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        repo_paths[f"slug{i}"] = repo_dir

    mirror_paths = {}
    for i in range(3):
        mirror_dir = tmp_path / f"mirror{i}"
        mirror_dir.mkdir()
        mirror_paths[f"mslug{i}"] = mirror_dir

    _write_registry(reg_dir, repo_paths, mirror_paths)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    # `_tier_a5`'s tail (`_sort_unique`) shells out to the `sort` binary for
    # byte-parity with the bash oracle — a pre-existing, unrelated spawn
    # (out of this fix's scope; see `_sort_unique`'s own docstring). Stub it
    # out so this test isolates the registry-read path this fix targets
    # (`_merged_flat_registry`'s elimination of the `machine-local`
    # CLI spawns) rather than that separate, already-accounted-for spawn.
    monkeypatch.setattr(m, "_sort_unique", lambda lines: sorted(set(lines)))

    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    a5_out = m._tier_a5()
    mirror_keys = m._publish_mirror_keys()

    assert call_count["n"] == budget_entry["per_call"] == 0
    assert len(a5_out) == 5
    assert len(mirror_keys) == 3
