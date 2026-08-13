"""Characterization tests for coordinator_core.ops.validate_install_contract.

Port of: validate-install-contract.sh (coordinator-claude b5a4192c, 2026-07-20), 303 lines,
bash + jq — see the module's own Negative-spec docstring for what is
deliberately preserved vs. deliberately different.

Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.validate_install_contract import main


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
    assert rc == 0, capsys.readouterr().err


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
            "override": {"flag": "--claude-klabauter-root"},  # env missing
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
            "override": {"flag": "--claude-klabauter-root", "env": "CLAUDE_KLABAUTER_ROOT"},
        }
    ]
    path = _write_manifest(tmp_path, manifest)
    rc = main(["--manifest-path", str(path)])
    assert rc == 0, capsys.readouterr().err


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
