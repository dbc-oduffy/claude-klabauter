"""
coordinator_core.plugin_health.tests.test_oracle_surface

Coverage for the single shared definition of claude-klabauter's fleet-invocable
oracle surface (see oracle_surface.py's own module docstring for the
`fleet_reachability`/`bin_inventory_gate` shared-blind-spot incident this
module exists to close).

Spec backlink: commit f622297b90d98f7ccd8f5796b53fe034ab4b190d.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.plugin_health import oracle_surface as os_


def _write(path: Path, content: str = "#!/usr/bin/env python3\nprint('hi')\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_live_oracle_names_unions_across_all_given_dirs(tmp_path: Path):
    agent_bin = tmp_path / "coordinator" / "bin"
    repo_root_bin = tmp_path / "bin"
    coordinator_lib = tmp_path / "coordinator" / "lib"

    _write(agent_bin / "foo.py")
    _write(repo_root_bin / "claude-klabauter-doctor-probe.py")
    _write(coordinator_lib / "some-lib-oracle.py")

    names = os_.live_oracle_names([agent_bin, repo_root_bin, coordinator_lib])

    assert names == {"foo", "claude-klabauter-doctor-probe", "some-lib-oracle"}


def test_generated_windows_siblings_excluded_across_all_three_dirs(tmp_path: Path):
    """A `.cmd`/`.ps1` Windows-launcher twin must never surface as an
    independent oracle name in ANY of the three directories -- the widened
    surface must not regress the exclusion `_derive_agent_helper_target_map`
    already enforces per-directory."""
    agent_bin = tmp_path / "coordinator" / "bin"
    repo_root_bin = tmp_path / "bin"
    coordinator_lib = tmp_path / "coordinator" / "lib"

    _write(agent_bin / "foo.py")
    _write(agent_bin / "foo.cmd", "@echo off\n")
    _write(repo_root_bin / "shell-init-guard.py")
    _write(repo_root_bin / "shell-init-guard.cmd", "@echo off\n")
    _write(coordinator_lib / "resolve-coordinator-clone.py")
    _write(coordinator_lib / "resolve-coordinator-clone.cmd", "@echo off\n")
    _write(coordinator_lib / "resolve-coordinator-clone.ps1", "# ps1\n")

    names = os_.live_oracle_names([agent_bin, repo_root_bin, coordinator_lib])

    assert names == {"foo", "shell-init-guard", "resolve-coordinator-clone"}
    assert "foo.cmd" not in names
    assert "shell-init-guard.cmd" not in names
    assert "resolve-coordinator-clone.cmd" not in names
    assert "resolve-coordinator-clone.ps1" not in names


def test_reserved_name_restored_from_any_scanned_directory(tmp_path: Path):
    """`resolve-coordinator-clone` is popped by `_derive_agent_helper_target_map`
    from EVERY directory's own mapping (it is a reserved, differently
    -installed name -- see `_AGENT_HELPER_RESERVED_NAMES`), so a caller
    scanning only `coordinator/lib/` (where its real oracle lives) would
    otherwise see it vanish entirely. `live_oracle_names` must restore it by
    checking real disk existence, independent of that pop."""
    coordinator_lib = tmp_path / "coordinator" / "lib"
    _write(coordinator_lib / "resolve-coordinator-clone.py")

    names = os_.live_oracle_names([coordinator_lib])

    assert "resolve-coordinator-clone" in names


def test_resolve_extra_oracle_dirs_returns_repo_root_bin_and_coordinator_lib():
    dirs = os_.resolve_extra_oracle_dirs(claude_klabauter_root=Path("/some/claude-klabauter/root"))

    assert dirs == [
        Path("/some/claude-klabauter/root") / "bin",
        Path("/some/claude-klabauter/root") / "coordinator" / "lib",
    ]


def test_resolve_agent_bin_none_when_directory_absent(tmp_path: Path):
    assert os_.resolve_agent_bin(claude_klabauter_root=tmp_path) is None


def test_resolve_agent_bin_present_when_directory_exists(tmp_path: Path):
    (tmp_path / "coordinator" / "bin").mkdir(parents=True)

    assert os_.resolve_agent_bin(claude_klabauter_root=tmp_path) == tmp_path / "coordinator" / "bin"
