"""Tests for docs/install/agent-install-manifest.json's console-entrypoint
registration -- AC5 of docs/plans/2026-08-06-makima-ize-the-survey-census.md.

Covers manifest shape only (static JSON assertions); live fresh-install
evidence for AC5 is EM-run and cited in the AC table, not exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "install" / "agent-install-manifest.json"
)


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_is_valid_json() -> None:
    _load_manifest()


def test_console_entrypoint_registered_as_system_prerequisite() -> None:
    manifest = _load_manifest()
    prereqs = manifest["system_prerequisites"]
    matches = [p for p in prereqs if p.get("id") == "coordinator_invoke_console_script"]
    assert len(matches) == 1, (
        "expected exactly one coordinator_invoke_console_script entry in "
        "system_prerequisites"
    )


def test_console_entrypoint_probe_invokes_console_script_without_repo_flag() -> None:
    manifest = _load_manifest()
    prereqs = manifest["system_prerequisites"]
    entry = next(p for p in prereqs if p["id"] == "coordinator_invoke_console_script")

    assert entry["tier"] == "hard"

    probe = entry["probe"]
    assert probe["kind"] == "command_succeeds"
    assert probe["cmd"].startswith("coordinator-invoke ping")
    assert "--repo" not in probe["cmd"], (
        "ping is a none-scoped op (op_scopes.py); the probe must not pass "
        "--repo, which invoke/__main__.py refuses on none-scoped ops"
    )


def test_console_entrypoint_remediation_names_pyproject_and_setup() -> None:
    manifest = _load_manifest()
    prereqs = manifest["system_prerequisites"]
    entry = next(p for p in prereqs if p["id"] == "coordinator_invoke_console_script")

    remediation = entry["install"]["remediation"]
    assert entry["install"]["mode"] == "bundled"
    assert "pyproject.toml" in remediation
    assert "scripts/setup.py" in remediation


def test_manifest_still_declares_coordinator_core_prerequisite() -> None:
    """The new entry must not have displaced the existing coordinator_core probe."""
    manifest = _load_manifest()
    prereqs = manifest["system_prerequisites"]
    ids = [p.get("id") for p in prereqs]
    assert "coordinator_core" in ids
    assert "coordinator_invoke_console_script" in ids
