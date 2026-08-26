"""Characterization tests for coordinator_core.ops.validate_install_contract.

Port of: validate-install-contract.sh (DoE b5a4192c, 2026-07-20), 303 lines,
bash + jq — see the module's own Negative-spec docstring for what is
deliberately preserved vs. deliberately different.

Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import validate_install_contract as vic
from coordinator_core.ops.validate_install_contract import main

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

def _write_record(records_root: Path, platform: str, machine: str, surface: str, **overrides) -> Path:
    """Write one platform-outcome record conforming to
    coordinator/schemas/platform-outcome.schema.json's required fields, with
    per-field overrides for stale/failing variants."""
    record = {
        "platform": platform,
        "surface": surface,
        "command": "python3 setup.py --check-only",
        "outcome": "pass",
        "exit_code": 0,
        "observed_at": "2026-08-14T00:00:00Z",
        "machine": machine,
        "surface_sha": "deadbeef" * 5,
        "invoking_repo": "claude-klabauter",
    }
    record.update(overrides)
    platform_dir = records_root / platform / machine
    platform_dir.mkdir(parents=True, exist_ok=True)
    path = platform_dir / f"{surface}.yaml"
    path.write_text(yaml.safe_dump(record), encoding="utf-8")
    return path


def _write_manifest(tmp_path: Path, data: object, name: str = "manifest.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _compliant_manifest() -> dict:
    """A manifest that passes points 1/2/3/4/6 cleanly."""
    return {
        "packageability_compliance": {"declared": True},
        "required_env_vars": [],
        "direct_deps": [
            {"id": "sibling", "functional_probe": {"kind": "sibling_dir_exists"}},
        ],
        "system_prerequisites": [
            {"id": "jq", "probe": {"cmd": "jq --version"}},
        ],
        "programmatic_entry_point": {
            "entry_point_contract": {"non_interactive_flag": "--non-interactive"},
        },
        "installer_floor": {"floor_prerequisite_ids": ["jq"]},
        "tested_platforms": ["macos"],
        "configurable_locations": [
            {
                "name": "install_root",
                "discovery": {"candidates": ["~/.claude"]},
                "default": "~/.claude",
                "override": {"flag": "--install-root", "env": "INSTALL_ROOT"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# CLI arg parsing
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage: validate-install-contract.sh" in out


def test_unrecognized_argument_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unrecognized argument: --bogus" in err


def test_manifest_path_missing_value_exits_one(capsys):
    """Defensive improvement over the bash oracle's undefined `shift: shift
    count out of range` behavior on the same malformed input — see module
    Negative-spec. This is the regression test for that fixed edge (addendum
    rule 6)."""
    rc = main(["--manifest-path"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--manifest-path requires a value" in err


# ---------------------------------------------------------------------------
# Manifest presence / JSON validity / opt-in gate
# ---------------------------------------------------------------------------


def test_no_manifest_declared_skips_clean(tmp_path, capsys):
    rc = main(["--manifest-path", str(tmp_path / "nope.json")])
    assert rc == 0
    err = capsys.readouterr().err
    assert "SKIP: no agent-install-manifest.json declared at:" in err


def test_invalid_json_exits_one(tmp_path, capsys):
    path = tmp_path / "manifest.json"
    path.write_text("{not valid json", encoding="utf-8")
    rc = main(["--manifest-path", str(path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "is not valid JSON" in err


def test_not_opted_in_skips_clean(tmp_path, capsys):
    path = _write_manifest(tmp_path, {"packageability_compliance": {"declared": False}})
    rc = main(["--manifest-path", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP CLEAN" in out


def test_missing_packageability_compliance_key_skips_clean(tmp_path, capsys):
    path = _write_manifest(tmp_path, {"foo": "bar"})
    rc = main(["--manifest-path", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP CLEAN" in out


def test_declared_string_true_not_accepted_type_strict(tmp_path, capsys):
    """Type-strict opt-in gate: a schema-invalid string "true" must not pass."""
    path = _write_manifest(tmp_path, {"packageability_compliance": {"declared": "true"}})
    rc = main(["--manifest-path", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP CLEAN" in out


def test_non_object_manifest_skips_clean(tmp_path, capsys):
    """A syntactically valid JSON array (not an object) mirrors the bash
    oracle's jq-runtime-type-error-on-non-object -> SKIP CLEAN (see
    Negative-spec)."""
    path = _write_manifest(tmp_path, [1, 2, 3])
    rc = main(["--manifest-path", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP CLEAN" in out


# ---------------------------------------------------------------------------
# Fully compliant manifest
# ---------------------------------------------------------------------------


def test_fully_compliant_manifest_exits_zero(tmp_path, capsys):
    path = _write_manifest(tmp_path, _compliant_manifest())
    rc = main(["--manifest-path", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out
    assert "packageability-compliant" in out


# ---------------------------------------------------------------------------
# Point 1
# ---------------------------------------------------------------------------


def test_point1_missing_required_env_vars(tmp_path, capsys):
    manifest = _compliant_manifest()
    del manifest["required_env_vars"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-1]: required_env_vars is missing entirely" in err


def test_point1_direct_dep_missing_functional_probe(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["direct_deps"] = [{"id": "no-probe"}]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "direct_deps[0] ('no-probe') has no functional_probe.kind" in err


def test_point1_direct_dep_accepts_flat_functional_probe_kind(tmp_path, capsys):
    """coordinator-claude's own manifest shape (flat functional_probe_kind, not
    nested functional_probe.kind) is a CONFORMING shape, not a failure (W0.5
    Option B+C, 2026-07-19) -- see agent-install-manifest.schema.json's
    DirectDep sub-schema."""
    manifest = _compliant_manifest()
    manifest["direct_deps"] = [
        {"id": "claude-klabauter", "functional_probe_kind": "claude_klabauter_seam_resolvable"}
    ]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert "has no functional_probe" not in err
    assert rc == 0


def test_point1_direct_dep_rejects_empty_flat_functional_probe_kind(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["direct_deps"] = [{"id": "no-probe", "functional_probe_kind": ""}]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "direct_deps[0] ('no-probe') has no functional_probe.kind" in err


def test_point1_system_prereq_missing_probe_cmd(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["system_prerequisites"] = [{"id": "unnamed-thing"}]
    manifest["installer_floor"] = {"floor_prerequisite_ids": []}
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "system_prerequisites[0] ('unnamed-thing') has no probe.cmd" in err


def test_point1_unnamed_dep_index_fallback(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["direct_deps"] = [{}]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "direct_deps[0] ('(unnamed, index 0)') has no functional_probe.kind" in err


# ---------------------------------------------------------------------------
# Point 2
# ---------------------------------------------------------------------------


def test_point2_programmatic_entry_point_missing_contract(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["programmatic_entry_point"] = {}
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "programmatic_entry_point.entry_point_contract is missing" in err


def test_point2_programmatic_entry_point_neither_flag(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["programmatic_entry_point"] = {"entry_point_contract": {}}
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "declares neither non_interactive_flag nor check_only_flag" in err


def test_point2_falls_back_to_standalone_setup_script(tmp_path, capsys):
    manifest = _compliant_manifest()
    del manifest["programmatic_entry_point"]
    manifest["standalone_setup_script"] = {
        "entry_point_contract": {"check_only_flag": "--check-only"}
    }
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    assert rc == 0, capsys.readouterr().err


def test_point2_no_entry_point_at_all(tmp_path, capsys):
    manifest = _compliant_manifest()
    del manifest["programmatic_entry_point"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "standalone_setup_script.entry_point_contract is missing" in err


# ---------------------------------------------------------------------------
# Point 3
# ---------------------------------------------------------------------------


def test_point3_orphan_floor_prerequisite_id(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["installer_floor"] = {"floor_prerequisite_ids": ["ghost"]}
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "installer_floor.floor_prerequisite_ids contains 'ghost'" in err


def test_point3_no_installer_floor_key_skips_check(tmp_path, capsys):
    manifest = _compliant_manifest()
    del manifest["installer_floor"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    assert rc == 0, capsys.readouterr().err


# ---------------------------------------------------------------------------
# Point 4
# ---------------------------------------------------------------------------


def test_point4_missing_tested_platforms(tmp_path, capsys):
    manifest = _compliant_manifest()
    del manifest["tested_platforms"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tested_platforms is missing or empty" in err


def test_point4_empty_tested_platforms_is_valid(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = []
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "point-4" not in err


def test_point4_empty_tested_platforms_no_records_consulted(tmp_path, capsys, monkeypatch):
    """Empty array is trivially satisfied — derive_tested_platforms must not
    even be invoked (no records dir needed, no I/O)."""
    def _boom(*a, **k):
        raise AssertionError("derive_tested_platforms must not be called for []")

    monkeypatch.setattr(vic, "derive_tested_platforms", _boom)
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = []
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    assert rc == 0, capsys.readouterr().err


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_point4_absent_records_dir_is_not_a_finding(tmp_path, capsys):
    """Greenfield repo: state/platform-outcomes/ does not exist at all. Must
    NOT hard-fail — regressing this would undo commit 9a10aba9's unblock.

    Spawns a real external process (current_repo_sha's `git rev-parse`, left
    un-monkeypatched here). Spawn ratchet:
    coordinator_core/tests/test_no_new_spawning_tests.py"""
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos", "windows"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "point-4" not in err


def test_point4_pyyaml_unavailable_is_not_a_finding(tmp_path, capsys, monkeypatch):
    """PyYAML unavailable => cannot verify != violation; must not fail."""
    monkeypatch.setattr(vic, "current_repo_sha", lambda root: None)

    def _raise_import_error(*a, **k):
        raise ModuleNotFoundError("no module named 'yaml'")

    monkeypatch.setattr(vic, "derive_tested_platforms", _raise_import_error)
    records_root = tmp_path / "state" / "platform-outcomes"
    _write_record(records_root, "macos", "mac1", "programmatic_entry_point")
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    assert rc == 0, capsys.readouterr().err


def test_point4_present_valid_passing_fresh_record_no_finding(tmp_path, capsys, monkeypatch):
    fixed_sha = "cafebabe" * 5
    monkeypatch.setattr(vic, "current_repo_sha", lambda root: fixed_sha)
    records_root = tmp_path / "state" / "platform-outcomes"
    _write_record(
        records_root,
        "macos",
        "mac1",
        "programmatic_entry_point",
        surface_sha=fixed_sha,
    )
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "point-4" not in err


def test_point4_stale_surface_sha_mismatch_finding(tmp_path, capsys, monkeypatch):
    fixed_sha = "cafebabe" * 5
    monkeypatch.setattr(vic, "current_repo_sha", lambda root: fixed_sha)
    records_root = tmp_path / "state" / "platform-outcomes"
    _write_record(
        records_root,
        "macos",
        "mac1",
        "programmatic_entry_point",
        surface_sha="stalestale" * 4,
    )
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tested_platforms declares 'macos', but no passing, fresh platform-outcome record backs it" in err


def test_point4_stale_observed_at_finding(tmp_path, capsys, monkeypatch):
    """SECONDARY staleness: >30-day-old observed_at, even with a matching
    surface_sha (achieved here via current_sha=None so PRIMARY never fires)."""
    monkeypatch.setattr(vic, "current_repo_sha", lambda root: None)
    records_root = tmp_path / "state" / "platform-outcomes"
    _write_record(
        records_root,
        "macos",
        "mac1",
        "programmatic_entry_point",
        observed_at="2020-01-01T00:00:00Z",
    )
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tested_platforms declares 'macos'" in err


def test_point4_failing_record_finding(tmp_path, capsys, monkeypatch):
    fixed_sha = "cafebabe" * 5
    monkeypatch.setattr(vic, "current_repo_sha", lambda root: fixed_sha)
    records_root = tmp_path / "state" / "platform-outcomes"
    _write_record(
        records_root,
        "macos",
        "mac1",
        "programmatic_entry_point",
        surface_sha=fixed_sha,
        outcome="fail",
        exit_code=1,
    )
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "tested_platforms declares 'macos'" in err


def test_point4_ceremony_surface_is_grandfathered_not_backing_evidence(tmp_path, capsys, monkeypatch):
    """A record whose `surface` is a ceremony op id (not an entry-point key)
    is not backing evidence -> platform has ZERO entry-point-surface records
    -> grandfathered (already-declared claim preserved), no finding."""
    fixed_sha = "cafebabe" * 5
    monkeypatch.setattr(vic, "current_repo_sha", lambda root: fixed_sha)
    records_root = tmp_path / "state" / "platform-outcomes"
    _write_record(
        records_root,
        "macos",
        "mac1",
        "some_ceremony_op_id",
        surface_sha=fixed_sha,
    )
    manifest = _compliant_manifest()
    manifest["tested_platforms"] = ["macos"]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "point-4" not in err


# ---------------------------------------------------------------------------
# Point 6
# ---------------------------------------------------------------------------


def test_point6_missing_configurable_locations(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["configurable_locations"] = []
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "configurable_locations is missing or empty" in err


def test_point6_location_missing_candidates_default_override(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["configurable_locations"] = [{"name": "bare"}]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "configurable_locations[0] ('bare') has no ranked discovery.candidates" in err
    assert "configurable_locations[0] ('bare') has no default" in err
    assert "configurable_locations[0] ('bare') has no override.flag or override.env" in err


def test_point6_dependency_root_requires_both_flag_and_env(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["configurable_locations"] = [
        {
            "name": "claude_klabauter_root",
            "dependency_id": "claude-klabauter",
            "discovery": {"candidates": ["../claude-klabauter"]},
            "default": "../claude-klabauter",
            "override": {"flag": "--claude-klabauter-live-root"},  # env missing
        }
    ]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    err = capsys.readouterr().err
    assert rc == 1
    assert (
        "configurable_locations[0] ('claude_klabauter_root') is a dependency root "
        "(dependency_id set) but its override lacks a flag and/or env" in err
    )


def test_point6_dependency_root_with_both_flag_and_env_passes(tmp_path, capsys):
    manifest = _compliant_manifest()
    manifest["configurable_locations"] = [
        {
            "name": "claude_klabauter_root",
            "dependency_id": "claude-klabauter",
            "discovery": {"candidates": ["../claude-klabauter"]},
            "default": "../claude-klabauter",
            "override": {"flag": "--claude-klabauter-live-root", "env": "CLAUDE_KLABAUTER_ROOT"},
        }
    ]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    assert rc == 0, capsys.readouterr().err


def test_point6_real_manifest_engine_door_installed_entry_passes(capsys):
    """The real agent-install-manifest.json's engine_door_installed entry (added
    beside engine_warm_enabled, docs/plans/2026-08-22-warm-engine-and-door-install-
    from-published-root.md C7) must satisfy Point-6's shape gate — a new entry
    needs a test that fails if it regresses, not just an external gate that
    happens to run someday."""
    manifest_path = (
        Path(__file__).resolve().parents[2] / "docs" / "install" / "agent-install-manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    locations = manifest["configurable_locations"]
    names = [loc.get("name") for loc in locations]
    assert "engine_door_installed" in names

    failures = vic._Failures()
    vic._check_point6(manifest, failures)
    err = capsys.readouterr().err
    assert failures.count == 0, err

    entry = next(loc for loc in locations if loc.get("name") == "engine_door_installed")
    assert entry["discovery"]["candidates"]
    assert entry["default"]
    assert entry["override"].get("flag") or entry["override"].get("env")


# ---------------------------------------------------------------------------
# --repo-root default-path derivation
# ---------------------------------------------------------------------------


def test_repo_root_derives_default_manifest_path(tmp_path, capsys):
    repo_root = tmp_path / "repo"
    manifest_dir = repo_root / "coordinator" / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "agent-install-manifest.json").write_text(
        json.dumps(_compliant_manifest()), encoding="utf-8"
    )
    rc = main(["--repo-root", str(repo_root)])
    assert rc == 0, capsys.readouterr().err


def test_repo_root_probes_claude_klabauter_layout_when_doe_layout_absent(tmp_path, capsys):
    """claude-klabauter's own layout (`docs/install/...`, no `coordinator/` prefix) must
    resolve by default too — regression for the relpath that was hardcoded to
    the DoE-claude layout only, which made a no-args run in THIS repo always
    land on "no manifest declared" and exit 0 (green-by-skip on the guard for
    claude-klabauter's own manifest)."""
    repo_root = tmp_path / "repo"
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "agent-install-manifest.json").write_text(
        json.dumps(_compliant_manifest()), encoding="utf-8"
    )
    rc = main(["--repo-root", str(repo_root)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "OK:" in out
    assert str(manifest_dir / "agent-install-manifest.json") in out


def test_repo_root_prefers_doe_layout_when_both_present(tmp_path, capsys):
    """Both layouts probed, DoE-shaped first — matches the oracle's one and
    only prior default, so every existing DoE-shaped caller's behavior stays
    byte-identical."""
    repo_root = tmp_path / "repo"
    doe_dir = repo_root / "coordinator" / "docs" / "install"
    doe_dir.mkdir(parents=True)
    doe_manifest = doe_dir / "agent-install-manifest.json"
    doe_manifest.write_text(json.dumps(_compliant_manifest()), encoding="utf-8")

    claude_klabauter_dir = repo_root / "docs" / "install"
    claude_klabauter_dir.mkdir(parents=True)
    (claude_klabauter_dir / "agent-install-manifest.json").write_text(
        json.dumps({"packageability_compliance": {"declared": False}}), encoding="utf-8"
    )

    rc = main(["--repo-root", str(repo_root)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert str(doe_manifest) in out


def test_repo_root_skip_message_names_both_probed_paths(tmp_path, capsys):
    repo_root = tmp_path / "repo"
    rc = main(["--repo-root", str(repo_root)])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "SKIP: no agent-install-manifest.json declared at any of:" in err
    assert str(repo_root / "coordinator" / "docs" / "install" / "agent-install-manifest.json") in err
    assert str(repo_root / "docs" / "install" / "agent-install-manifest.json") in err


# ---------------------------------------------------------------------------
# Point 2 — declared setup-script paths must resolve on disk
#
# Regression origin: DoE-claude's manifest reported `packageability-compliant`
# while its declared Point-2 entry point had left the repo entirely. Point 2
# verified that the field and its flags were DECLARED, never that the path
# resolved, so a migration of 1135 files moved the target and nothing noticed.
# ---------------------------------------------------------------------------


def _manifest_with_setup_script(**paths) -> dict:
    manifest = _compliant_manifest()
    manifest["standalone_setup_script"] = dict(paths)
    return manifest


def test_point2_declared_posix_setup_script_must_exist(tmp_path, capsys):
    (tmp_path / "scripts").mkdir()
    manifest = _manifest_with_setup_script(posix="scripts/gone.py")
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: standalone_setup_script.posix" in err
    assert "scripts/gone.py" in err


def test_point2_declared_windows_setup_script_must_exist(tmp_path, capsys):
    """The Windows leg is checked with equal force. A repoint that resolves only
    `posix` leaves Windows installing from a path that does not exist, and this
    repo treats both platforms as first-class."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(posix="scripts/setup.py", windows="scripts/setup.ps1")
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: standalone_setup_script.windows" in err


def test_point2_resolving_setup_script_paths_pass(tmp_path, capsys):
    """Quiet on clean — a check that fires on a healthy manifest is muted within
    a week."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(posix="scripts/setup.py", windows="scripts/setup.py")
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    assert rc == 0, capsys.readouterr().err


def test_point2_stats_the_programmatic_entry_point_too(tmp_path, capsys):
    """The authoritative witness gets stat-ed, not just the fallback.

    Point 2 treats `programmatic_entry_point` as authoritative — when present it
    wins outright and `standalone_setup_script` is not consulted for the witness
    check at all — yet the declared-path stat originally covered only
    `standalone_setup_script`. So the field the contract trusts MOST was the one
    field nothing verified, and a manifest could pass Point 2 while its
    authoritative entry point had left the repo. Found live against DoE-claude's
    manifest on 2026-08-17: its `programmatic_entry_point.posix` names a file
    present in neither their working tree nor the published mirror.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(posix="scripts/setup.py")
    manifest["programmatic_entry_point"] = {
        "posix": "scripts/install-maximalist.py",
        "entry_point_contract": {"check_only_flag": "--check-only"},
    }
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: programmatic_entry_point.posix" in err
    assert "scripts/install-maximalist.py" in err


def test_point2_resolving_programmatic_entry_point_passes(tmp_path, capsys):
    """Quiet on clean, and proves the failure above is the path check firing —
    not the new field being rejected outright."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    (scripts / "install-maximalist.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(posix="scripts/setup.py")
    manifest["programmatic_entry_point"] = {
        "posix": "scripts/install-maximalist.py",
        "entry_point_contract": {"check_only_flag": "--check-only"},
    }
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    assert rc == 0, capsys.readouterr().err


def test_point2_object_valued_platform_leg_fails_not_skips(tmp_path, capsys):
    """The cross-repo object form must not buy a silent pass.

    Regression for the bypass this guard was itself vulnerable to: the `str`
    check exists to protect the `Path` join, and treating a non-string as
    "nothing to verify" let an object-valued leg skip the stat entirely — the
    manifest then passed Point 2 declaring an entry point nothing confirmed.
    Silent green, from the check written to close silent green.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(
        posix={"repo": "claude_klabauter", "path": "scripts/setup.py"},
        windows="scripts/setup.py",
    )
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: standalone_setup_script.posix" in err
    assert "dict" in err


def test_point2_empty_string_platform_leg_fails(tmp_path, capsys):
    """An empty string is DECLARED but unusable — the same vacuous pass in a
    cheaper disguise, and it must fail rather than skip."""
    manifest = _manifest_with_setup_script(posix="")
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: standalone_setup_script.posix" in err


def test_point2_object_valued_programmatic_entry_point_leg_fails_not_skips(tmp_path, capsys):
    """The object-form bypass applies to the authoritative witness too.

    `_stat_declared_paths` is one shared helper parametrized by field — the
    `standalone_setup_script` regression above proved the str-guard fires: this
    is the same case for `programmatic_entry_point`, the field Point 2 trusts
    MOST and the one a live manifest (DoE-claude, 2026-08-17) actually had
    unresolved.
    """
    manifest = _compliant_manifest()
    manifest["programmatic_entry_point"] = {
        "posix": {"repo": "claude_klabauter", "path": "scripts/install-maximalist.py"},
        "entry_point_contract": {"check_only_flag": "--check-only"},
    }
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: programmatic_entry_point.posix" in err
    assert "dict" in err


def test_point2_empty_string_programmatic_entry_point_leg_fails(tmp_path, capsys):
    """Same vacuous-pass-in-cheaper-disguise case as `standalone_setup_script`,
    for the authoritative witness field."""
    manifest = _compliant_manifest()
    manifest["programmatic_entry_point"] = {
        "posix": "",
        "entry_point_contract": {"check_only_flag": "--check-only"},
    }
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: programmatic_entry_point.posix" in err


def test_point2_declared_windows_programmatic_entry_point_must_exist(tmp_path, capsys):
    """The Windows leg is checked with equal force on the authoritative witness
    too — mirrors `test_point2_declared_windows_setup_script_must_exist`, which
    only ever exercised `standalone_setup_script`."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install-maximalist.py").write_text("", encoding="utf-8")
    manifest = _compliant_manifest()
    manifest["programmatic_entry_point"] = {
        "posix": "scripts/install-maximalist.py",
        "windows": "scripts/install-maximalist.ps1",
        "entry_point_contract": {"check_only_flag": "--check-only"},
    }
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 1
    assert "FAIL [point-2]: programmatic_entry_point.windows" in err


def test_point2_absent_platform_leg_stays_a_legitimate_skip(tmp_path, capsys):
    """Negative-spec boundary: the fix above must not turn an ABSENT key into a
    failure. A platform served another way declares nothing, and the remediation
    text for a bad value explicitly offers key removal as a fix — so removing it
    has to actually work."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(posix="scripts/setup.py")
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    assert rc == 0, capsys.readouterr().err


def test_point2_ignores_non_path_keys_on_the_setup_script_block(tmp_path, capsys):
    """`_comment_*` keys and any future non-path metadata are not paths and must
    not be stat-ed — only the declared platform legs are."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "setup.py").write_text("", encoding="utf-8")
    manifest = _manifest_with_setup_script(
        posix="scripts/setup.py",
        _comment_standalone_setup_script="prose that is not a path",
    )
    path = _write_manifest(tmp_path, manifest)

    rc = main(["--manifest-path", str(path), "--repo-root", str(tmp_path)])

    assert rc == 0, capsys.readouterr().err
