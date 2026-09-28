"""
coordinator_core.ops.dispatch_emit.tests.test_admission_config

Covers admission.py's config-precedence resolver (`load_thresholds` /
`_load_thresholds_with_missing`): machine-local `workflow_admission.<key>`
over the emitting repo's `coordinator.local.md` `workflow_admission:` map,
the `.git`-ancestor walk to find that file, malformed/negative-value
handling, and the no-stale-cache contract. Isolated via the resolver's own
registry-dir seam (`MACHINE_LOCAL_REGISTRY_DIR`), never the real store.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import admission


@pytest.fixture
def registry_dir(tmp_path, monkeypatch):
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    return reg_dir


def _write_registry(reg_dir: Path, filename: str, body: str) -> None:
    (reg_dir / filename).write_text(textwrap.dedent(body), encoding="utf-8")


def _write_local_md(repo_root: Path, body: str) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / ".git").mkdir(exist_ok=True)
    (repo_root / "coordinator.local.md").write_text(
        "---\n" + textwrap.dedent(body) + "\n---\nbody\n", encoding="utf-8"
    )


_FULL_LOCAL_MD_BLOCK = """\
workflow_admission:
  cpu_load_max: 0.9
  mem_avail_pct_min: 10
  max_hold_s: 90
  recheck_s: 10
"""


def test_coordinator_local_md_only(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    _write_local_md(repo_root, _FULL_LOCAL_MD_BLOCK)
    t = admission.load_thresholds(repo_root)
    assert t is not None
    assert t.cpu_load_max == 0.9
    assert t.mem_avail_pct_min == 10.0
    assert t.max_hold_s == 90.0
    assert t.recheck_s == 10.0
    assert set(t.source.values()) == {"coordinator.local.md"}


def test_machine_local_only(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    _write_registry(
        registry_dir,
        "registry.toml",
        """\
        [workflow_admission]
        cpu_load_max = 1.2
        mem_avail_pct_min = 15
        max_hold_s = 60
        recheck_s = 5
        """,
    )
    t = admission.load_thresholds(repo_root)
    assert t is not None
    assert t.cpu_load_max == 1.2
    assert set(t.source.values()) == {"machine-local"}


def test_per_key_mix_machine_local_wins_source_records_both(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    _write_local_md(repo_root, _FULL_LOCAL_MD_BLOCK)
    _write_registry(
        registry_dir,
        "registry.toml",
        """\
        [workflow_admission]
        cpu_load_max = 1.5
        """,
    )
    t = admission.load_thresholds(repo_root)
    assert t is not None
    assert t.cpu_load_max == 1.5
    assert t.source["cpu_load_max"] == "machine-local"
    assert t.mem_avail_pct_min == 10.0
    assert t.source["mem_avail_pct_min"] == "coordinator.local.md"


def test_partial_keys_returns_none_with_missing_names(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    _write_local_md(
        repo_root,
        """\
        workflow_admission:
          cpu_load_max: 0.9
          recheck_s: 10
        """,
    )
    thresholds, missing = admission._load_thresholds_with_missing(repo_root)
    assert thresholds is None
    assert set(missing) == {"mem_avail_pct_min", "max_hold_s"}


def test_non_numeric_value_treated_as_missing(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    _write_local_md(
        repo_root,
        """\
        workflow_admission:
          cpu_load_max: not-a-number
          mem_avail_pct_min: 10
          max_hold_s: 90
          recheck_s: 10
        """,
    )
    thresholds, missing = admission._load_thresholds_with_missing(repo_root)
    assert thresholds is None
    assert "cpu_load_max" in missing


def test_negative_value_treated_as_missing(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    _write_local_md(
        repo_root,
        """\
        workflow_admission:
          cpu_load_max: -1
          mem_avail_pct_min: 10
          max_hold_s: 90
          recheck_s: 10
        """,
    )
    thresholds, missing = admission._load_thresholds_with_missing(repo_root)
    assert thresholds is None
    assert "cpu_load_max" in missing


def test_local_md_found_from_nested_start_dir_via_git_ancestor_walk(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    _write_local_md(repo_root, _FULL_LOCAL_MD_BLOCK)
    nested = repo_root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    t = admission.load_thresholds(nested)
    assert t is not None
    assert t.cpu_load_max == 0.9


def test_no_subprocess_spawn_during_load_thresholds(tmp_path, registry_dir, monkeypatch):
    repo_root = tmp_path / "repo"
    _write_local_md(repo_root, _FULL_LOCAL_MD_BLOCK)

    def _boom(*args, **kwargs):
        raise AssertionError("load_thresholds must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    t = admission.load_thresholds(repo_root)
    assert t is not None


def test_machine_local_key_written_between_two_calls_is_seen_no_stale_cache(tmp_path, registry_dir):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / ".git").mkdir()
    _write_registry(
        registry_dir,
        "registry.toml",
        """\
        [workflow_admission]
        cpu_load_max = 0.9
        mem_avail_pct_min = 10
        max_hold_s = 90
        recheck_s = 10
        """,
    )
    first = admission.load_thresholds(repo_root)
    assert first is not None and first.cpu_load_max == 0.9

    _write_registry(
        registry_dir,
        "registry.toml",
        """\
        [workflow_admission]
        cpu_load_max = 0.5
        mem_avail_pct_min = 10
        max_hold_s = 90
        recheck_s = 10
        """,
    )
    second = admission.load_thresholds(repo_root)
    assert second is not None and second.cpu_load_max == 0.5


def test_unconfigured_when_no_git_root_found(tmp_path, registry_dir):
    lonely = tmp_path / "no-git-anywhere"
    lonely.mkdir()
    t = admission.load_thresholds(lonely)
    assert t is None
