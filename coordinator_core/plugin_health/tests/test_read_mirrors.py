"""
coordinator_core.plugin_health.tests.test_read_mirrors — dedicated coverage for
read_all_mirrors(), the plugin.mirrors registry-TOML parser folded into
coordinator_core.plugin_health.drift during the check-plugin-drift.sh port.

The function itself already lives at coordinator_core/plugin_health/drift.py
(landed as part of the drift-probe port, which absorbed the formerly-standalone
DoE-claude coordinator/bin/lib/read-mirrors.sh — see that module's docstring).
This file closes a coverage gap: drift.py's existing test suite (test_drift.py)
never exercised read_all_mirrors() directly. Parity verified against the live
bash oracle (_read_all_mirrors in read-mirrors.sh) on matching fixtures during
this port's parity gate.

Port of: read-mirrors.sh::_read_all_mirrors (DoE 721a71f4, 2026-07-21)
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.plugin_health.drift import read_all_mirrors

_NESTED_AND_FLAT_TOML = """\
schema = 1
"plugin.mirrors.example-game-repo.propagation_mode" = "copy_install"
"plugin.mirrors.example-game-repo.source_path" = "/src/example-game-repo"
"plugin.mirrors.example-game-repo.live_path" = "/live/example-game-repo"
"plugin.mirrors.example-retrieval-repo.track_ref" = "origin/dev"

[plugin.mirrors.coordinator-claude]
propagation_mode = "source_is_live"
source_path = "/src/coordinator"
live_path = "/live/coordinator"

[plugin.mirrors.example-retrieval-repo]
propagation_mode = "editable_sibling_venv"
source_path = "/src/example-retrieval-repo"
"""


def test_nested_table_form(tmp_path: Path) -> None:
    reg = tmp_path / "registry.local.toml"
    reg.write_text(_NESTED_AND_FLAT_TOML, encoding="utf-8")
    mirrors = read_all_mirrors(reg)
    assert mirrors["coordinator-claude"] == {
        "propagation_mode": "source_is_live",
        "source_path": "/src/coordinator",
        "live_path": "/live/coordinator",
        "track_ref": "origin/main",
        "dist_name": "coordinator_claude",
        "reverse_drift_cmd": "",
    }


def test_flat_dotted_key_form_only(tmp_path: Path) -> None:
    reg = tmp_path / "registry.local.toml"
    reg.write_text(_NESTED_AND_FLAT_TOML, encoding="utf-8")
    mirrors = read_all_mirrors(reg)
    assert mirrors["example-game-repo"] == {
        "propagation_mode": "copy_install",
        "source_path": "/src/example-game-repo",
        "live_path": "/live/example-game-repo",
        "track_ref": "origin/main",
        "dist_name": "example-game-repo",
        "reverse_drift_cmd": "",
    }


def test_nested_wins_over_flat_on_conflicting_field(tmp_path: Path) -> None:
    # example-retrieval-repo has both a [plugin.mirrors.example-retrieval-repo] table (no track_ref)
    # AND a flat "plugin.mirrors.example-retrieval-repo.track_ref" key. The nested-table
    # entry doesn't define track_ref, so the flat value fills it in — but
    # live_path is present in neither, so it stays default-empty (nested
    # table wins on any field it DOES define; this fixture has no direct
    # collision, matching the bash oracle's own test corpus).
    reg = tmp_path / "registry.local.toml"
    reg.write_text(_NESTED_AND_FLAT_TOML, encoding="utf-8")
    mirrors = read_all_mirrors(reg)
    assert mirrors["example-retrieval-repo"]["track_ref"] == "origin/dev"
    assert mirrors["example-retrieval-repo"]["live_path"] == ""
    assert mirrors["example-retrieval-repo"]["dist_name"] == "example_retrieval_repo"


def test_missing_registry_file_returns_empty_dict(tmp_path: Path) -> None:
    reg = tmp_path / "does-not-exist.toml"
    assert read_all_mirrors(reg) == {}


def test_registry_with_no_mirrors_section_returns_empty_dict(tmp_path: Path) -> None:
    reg = tmp_path / "registry.local.toml"
    reg.write_text("schema = 1\n", encoding="utf-8")
    assert read_all_mirrors(reg) == {}


def test_dist_name_defaults_to_hyphen_to_underscore_of_plugin_name(tmp_path: Path) -> None:
    reg = tmp_path / "registry.local.toml"
    reg.write_text(
        "[plugin.mirrors.my-cool-plugin]\n"
        'propagation_mode = "source_is_live"\n',
        encoding="utf-8",
    )
    mirrors = read_all_mirrors(reg)
    assert mirrors["my-cool-plugin"]["dist_name"] == "my_cool_plugin"
    assert mirrors["my-cool-plugin"]["track_ref"] == "origin/main"


def test_merged_per_key_precedence(tmp_path: Path) -> None:
    """read_merged_mirrors: local wins per field, tracked fills gaps -- a
    plugin registered only in the tracked registry.toml is visible even when a
    registry.local.toml exists (the first-FILE-wins regression shape), and an
    explicit tracked field is never clobbered by a local-side synthesized
    default (defaults apply after the merge)."""
    from coordinator_core.plugin_health.drift import read_merged_mirrors

    local = tmp_path / "registry.local.toml"
    tracked = tmp_path / "registry.toml"
    local.write_text(
        "[plugin.mirrors.example-game-repo]\n"
        'live_path = "/local/live"\n',
        encoding="utf-8",
    )
    tracked.write_text(
        "[plugin.mirrors.example-game-repo]\n"
        'propagation_mode = "copy_install"\n'
        'live_path = "/tracked/live"\n'
        'track_ref = "origin/dev"\n'
        "[plugin.mirrors.tracked-only]\n"
        'propagation_mode = "source_is_live"\n'
        'source_path = "/src/tracked-only"\n',
        encoding="utf-8",
    )
    merged = read_merged_mirrors([local, tracked])
    assert merged["example-game-repo"]["live_path"] == "/local/live"  # local wins per field
    assert merged["example-game-repo"]["propagation_mode"] == "copy_install"  # tracked fills the gap
    assert merged["example-game-repo"]["track_ref"] == "origin/dev"  # explicit tracked value survives
    assert merged["tracked-only"]["propagation_mode"] == "source_is_live"
    assert merged["tracked-only"]["dist_name"] == "tracked_only"  # defaults post-merge
