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
(`discover_working_repos.py::_merged_flat_registry`) — zero `machine-local`
subprocesses, any key count.

TWO NAMED SHAPES (opro-03 C-08 finding, `state/audits/2026-08-19-opro-03-c08-
budgeted-op-spawn-trace.md` § 10): `_tier_a5` internally tail-calls
`_sort_unique`, which is a SANCTIONED carve-out (`_sort_unique` shells out to
`sort -u` for byte-parity with the bash oracle — named in
`coordinator_core/tests/test_no_bash_dependency.py`). A prior version of
`test_tier_a5_and_publish_mirror_keys_spawn_zero_subprocesses` stubbed
`_sort_unique` out before calling `_tier_a5()`, which silently absorbed that
sanctioned spawn into a manifest figure (`per_call: 0`) that claimed to be
"zero subprocesses" full stop — true of the stubbed op, not the real one.
This module now asserts two distinct manifest keys:

  - `spawn_count_budget.machine_local_cli_elimination_calls` — the isolated
    registry-read sub-path `_sort_unique` stubbed out. Proves
    `_merged_flat_registry` issues zero `machine-local` CLI spawns for any
    key count. This is the assertion the original test existed to protect.
  - `spawn_count_budget.per_call` — the REAL, unstubbed call: every
    `subprocess.run` invocation `_tier_a5()` + `_publish_mirror_keys()`
    actually make, `_sort_unique`'s sanctioned `sort -u` spawn included.
    Observed live at 1 (not 0) — `_sort_unique` makes exactly one
    `subprocess.run` call per non-empty input, independent of how many
    lines are being sorted; `_publish_mirror_keys` itself never calls
    `_sort_unique` and contributes 0.
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


def _seeded_registry(tmp_path, monkeypatch):
    """Shared fixture setup: 5 `repos.*` keys (real `.git` dirs) + 3
    `publish.mirrors.*.path` keys, registered via `MACHINE_LOCAL_REGISTRY_DIR`
    so `_merged_flat_registry()` reads them without any CLI spawn."""
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


def test_merged_flat_registry_eliminates_machine_local_cli_spawns(
    tmp_path, monkeypatch
) -> None:
    """Many `repos.*`/`publish.mirrors.*.path` keys must still cost exactly
    the manifest's `spawn_count_budget.machine_local_cli_elimination_calls`
    `subprocess.run` invocations — zero, regardless of key count (was O(N)
    `machine-local` CLI spawns before this fix). This isolates ONLY the
    registry-read sub-path `_merged_flat_registry` replaced; it deliberately
    does not observe `_tier_a5`'s own sanctioned `_sort_unique` tail spawn —
    see `test_tier_a5_and_publish_mirror_keys_real_spawn_count` below for
    that whole-op figure."""
    budget_entry = _manifest_spawn_budget()
    assert budget_entry["machine_local_cli_elimination_calls"] == 0

    _seeded_registry(tmp_path, monkeypatch)

    # `_tier_a5`'s tail (`_sort_unique`) shells out to `sort -u` for
    # byte-parity with the bash oracle — a SANCTIONED carve-out
    # (`coordinator_core/tests/test_no_bash_dependency.py` names
    # `discover_working_repos.py::_sort_unique` explicitly) counted
    # separately by `test_tier_a5_and_publish_mirror_keys_real_spawn_count`
    # below, via `spawn_count_budget.per_call`. Stub it out here so this
    # test isolates only the registry-read path this fix targets
    # (`_merged_flat_registry`'s elimination of the `machine-local` CLI
    # spawns), not that separate, separately-counted spawn.
    monkeypatch.setattr(m, "_sort_unique", lambda lines: sorted(set(lines)))

    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    a5_out = m._tier_a5()
    mirror_keys = m._publish_mirror_keys()

    assert call_count["n"] == budget_entry["machine_local_cli_elimination_calls"] == 0
    assert len(a5_out) == 5
    assert len(mirror_keys) == 3


def test_tier_a5_and_publish_mirror_keys_real_spawn_count(
    tmp_path, monkeypatch
) -> None:
    """The REAL, unstubbed `subprocess.run` count for calling `_tier_a5()`
    then `_publish_mirror_keys()` — nothing patched out. `_sort_unique`'s
    sanctioned `sort -u` carve-out spawn is counted here, not hidden: it
    fires exactly once (`_tier_a5`'s own tail call, line-count-independent —
    `_sort_unique` makes one `subprocess.run` call per non-empty input, not
    one per line); `_publish_mirror_keys` never calls `_sort_unique` and
    contributes zero. This is `spawn_count_budget.per_call` — the manifest
    key the original (pre-fix) test claimed to observe but did not, because
    it stubbed out the one function that spawns before measuring."""
    budget_entry = _manifest_spawn_budget()
    assert budget_entry["per_call"] == 1

    _seeded_registry(tmp_path, monkeypatch)

    call_count = {"n": 0}
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        call_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    a5_out = m._tier_a5()
    mirror_keys = m._publish_mirror_keys()

    assert call_count["n"] == budget_entry["per_call"] == 1
    assert len(a5_out) == 5
    assert len(mirror_keys) == 3
