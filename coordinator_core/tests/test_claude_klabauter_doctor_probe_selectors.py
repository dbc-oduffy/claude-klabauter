"""
coordinator_core.tests.test_claude_klabauter_doctor_probe_selectors — Tests for probe-selector flags.

Covers the selector surface of bin/claude-klabauter-doctor-probe.py:
  --triage         returns exactly the manifest's triage=true probe ids
  --cluster NAME   returns cluster-member probes (registry|install|dispatch)
  --probe <id>     returns exactly one probe by manifest id
  default          returns every manifest probe
  --step-zero      emits one NDJSON line per manifest probe; exits 0 or 1 (depends on probe outcomes)
  invalid --probe  exits nonzero (exit 2)
  --triage + --step-zero  argparse mutual-exclusion → exits nonzero

The roster and its triage partition are DERIVED from bin/doctor-probes.toml, never
restated here — see _manifest_probes()'s negative spec for why.

DELETED from code AND manifest (no entries, no stubs):
  claude-klabauter.core.uds.ping, claude-klabauter.shim.handshake, claude-klabauter.shim.harness_env,
  claude-klabauter.uds.socket_dir, claude-klabauter.health.mcp_exposed, claude-klabauter.coverage.seam
  (K-001, 2026-08-16, see state/kill-ledger.md — its sole referent,
  coordinator_core/ops/coverage_gate.py::run_coverage_gate, was removed)

Manifest clusters post-C5: registry, install, dispatch
  (ipc and coordinator clusters are gone — their probes were deleted)

Also covers the tomllib-absent / Python-version gate:
  _python_version_broken_envelope()  returns a valid schema_version 1 BROKEN envelope
  main() with _TOMLLIB_AVAILABLE=False  returns 1 and emits parseable JSON to stdout

All subprocess tests invoke the probe script as a subprocess (same invocation path as the
installer and bin/claude-klabauter-doctor-probe.py's direct CLI invocation) for integration-level
fidelity.

Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C6
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"

_MANIFEST = _REPO_ROOT / "bin" / "doctor-probes.toml"


def _manifest_probes() -> list[dict]:
    """Read the [[probe]] rows out of bin/doctor-probes.toml, the probe-registry SSOT.

    Negative spec: the expected id sets below are DERIVED from this manifest, never
    re-hardcoded. Three prior probe additions (claude-klabauter.schema.vendor_drift,
    claude-klabauter.commitments.recheck) each rotted a hand-maintained literal set and its
    embedded numeral; the selector contract under test is "the CLI honours the
    manifest's own `triage` flag and probe roster", not "the roster has N members".
    """
    import tomllib

    with _MANIFEST.open("rb") as fh:
        return tomllib.load(fh).get("probe", [])


# Every probe id in the manifest. The manifest carries no stub rows — a probe deleted
# from the code is deleted from the manifest — so manifest membership IS implemented.
_IMPLEMENTED_IDS = frozenset(p["id"] for p in _manifest_probes())

# triage=true probes per the manifest's own flag. triage=false probes (registry.key,
# coverage.seam, strategic.draft_staleness at time of writing) are excluded by the CLI.
_TRIAGE_IDS = frozenset(p["id"] for p in _manifest_probes() if p.get("triage") is True)


#: Wall-clock ceiling for one shelled doctor run. NOT a performance
#: assertion: `run_probes()` always exercises the full probe set (the
#: selector is a post-run filter), and that full set measured 45.7s on
#: this box BEFORE any warm probe existed, against a 50-70 concurrent
#: session load. A 60s cap left ~30% headroom over a pre-existing cost
#: and turned ordinary load into a red suite. The doctor's own runtime
#: is tracked as its own backlog item; this constant only stops that
#: cost from being reported here as a selector-logic failure.
_CLI_TIMEOUT_SECS = 300


def _run(*args: str) -> subprocess.CompletedProcess:
    """Run bin/claude-klabauter-doctor-probe.py with the given args; CLAUDE_KLABAUTER_ROOT set to repo root."""
    env = dict(os.environ)
    env["CLAUDE_KLABAUTER_ROOT"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(_BIN_PROBE), *args],
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_SECS,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ---------------------------------------------------------------------------
# Selector tests
# ---------------------------------------------------------------------------


class TestDoctorProbeSelectors:
    """CLI selector flags: --triage, --cluster, --probe, default, and mutual exclusion."""

    def test_selector_triage_returns_exactly_the_triage_flagged_ids(self) -> None:
        """--triage returns exactly the probe ids the manifest flags triage=true.

        Probes carrying triage=false (claude-klabauter.registry.key,
        claude-klabauter.strategic.draft_staleness at time of writing) must be excluded.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--triage")

        assert result.returncode == 0, (
            f"--triage exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}

        assert probe_ids == _TRIAGE_IDS, (
            f"Expected exactly {_TRIAGE_IDS!r}, got {probe_ids!r}"
        )

    def test_selector_cluster_registry_returns_registry_probes(self) -> None:
        """--cluster registry returns only registry-cluster probes.

        Registry cluster: claude-klabauter.root.resolve, claude-klabauter.registry.key.
        No ipc or coordinator clusters exist post-C5.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "registry")

        assert result.returncode == 0, (
            f"--cluster registry exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}

        # Must include both registry-cluster probes.
        expected_registry = {"claude-klabauter.root.resolve", "claude-klabauter.registry.key"}
        assert expected_registry <= probe_ids, (
            f"Missing registry probes: {expected_registry - probe_ids}"
        )

        # Must NOT include probes from other clusters.
        non_registry = {"claude-klabauter.core.import",
                        "claude-klabauter.resident.debris", "claude-klabauter.worktree.bloat",
                        "claude-klabauter.version.sanity", "claude-klabauter.invoke.smoke",
                        "claude-klabauter.schema.vendor_drift"}
        for pid in probe_ids:
            assert pid not in non_registry, (
                f"Non-registry probe {pid!r} appeared in --cluster registry output"
            )

    def test_selector_cluster_install_returns_install_probes(self) -> None:
        """--cluster install returns only install-cluster probes.

        Install cluster: claude-klabauter.core.import,
        claude-klabauter.resident.debris, claude-klabauter.worktree.bloat, claude-klabauter.version.sanity,
        claude-klabauter.strategic.draft_staleness, claude-klabauter.schema.vendor_drift.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "install")

        assert result.returncode == 0, (
            f"--cluster install exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}

        expected_install = {
            "claude-klabauter.core.import",
            "claude-klabauter.resident.debris",
            "claude-klabauter.worktree.bloat",
            "claude-klabauter.version.sanity",
            "claude-klabauter.strategic.draft_staleness",
            "claude-klabauter.schema.vendor_drift",
        }
        assert expected_install <= probe_ids, (
            f"Missing install probes: {expected_install - probe_ids}"
        )

        # Must NOT include probes from other clusters.
        non_install = {"claude-klabauter.root.resolve", "claude-klabauter.registry.key", "claude-klabauter.invoke.smoke"}
        for pid in probe_ids:
            assert pid not in non_install, (
                f"Non-install probe {pid!r} appeared in --cluster install output"
            )

    def test_selector_cluster_dispatch_returns_dispatch_probes(self) -> None:
        """--cluster dispatch returns only dispatch-cluster probes.

        Dispatch cluster: claude-klabauter.invoke.smoke (the spawn-per-call smoke probe).
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "dispatch")

        assert result.returncode == 0, (
            f"--cluster dispatch exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}

        assert "claude-klabauter.invoke.smoke" in probe_ids, (
            "claude-klabauter.invoke.smoke must appear in --cluster dispatch output"
        )

        # Must NOT include probes from other clusters.
        non_dispatch = {
            "claude-klabauter.root.resolve",
            "claude-klabauter.registry.key",
            "claude-klabauter.core.import",
            "claude-klabauter.resident.debris",
            "claude-klabauter.worktree.bloat",
            "claude-klabauter.version.sanity",
            "claude-klabauter.strategic.draft_staleness",
            "claude-klabauter.schema.vendor_drift",
        }
        for pid in probe_ids:
            assert pid not in non_dispatch, (
                f"Non-dispatch probe {pid!r} appeared in --cluster dispatch output"
            )

    def test_selector_probe_single_id_returns_exactly_one(self) -> None:
        """--probe claude-klabauter.core.import returns exactly that one probe."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.core.import")

        assert result.returncode == 0, (
            f"--probe claude-klabauter.core.import exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = [p["probe"] for p in envelope["probes"]]

        assert len(probe_ids) == 1, (
            f"Expected exactly 1 probe for --probe selector, got {len(probe_ids)}: {probe_ids}"
        )
        assert probe_ids[0] == "claude-klabauter.core.import", (
            f"Expected claude-klabauter.core.import, got {probe_ids[0]!r}"
        )

    def test_selector_default_returns_every_manifest_probe(self) -> None:
        """Default mode (no selector) returns every manifest probe in the envelope."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run()

        assert result.returncode == 0, (
            f"Default mode exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}

        # Review: code-reviewer — F3: was <= (subset); changed to == to catch unexpected extras.
        assert probe_ids == _IMPLEMENTED_IDS, (
            f"Probe set mismatch in default output: "
            f"missing={_IMPLEMENTED_IDS - probe_ids!r}, "
            f"unexpected={probe_ids - _IMPLEMENTED_IDS!r}"
        )

    def test_selector_invalid_probe_id_exits_nonzero(self) -> None:
        """--probe bogus.id exits 2 (unknown id rejected before probes run)."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "bogus.id")

        # exit 2 = argparse/manifest rejection before any probe runs (not exit 1 probe failure).
        # Review: code-reviewer — F4: was != 0; pinned to == 2 per documented contract.
        assert result.returncode == 2, (
            f"Expected exit 2 (argparse/manifest rejection) for unknown --probe id, "
            f"got {result.returncode}"
        )

    def test_selector_invalid_cluster_exits_nonzero(self) -> None:
        """--cluster unknown exits 2 (unknown cluster rejected before probes run)."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "unknown_cluster_xyz")

        # exit 2 = argparse/manifest rejection before any probe runs.
        assert result.returncode == 2, (
            f"Expected exit 2 (argparse/manifest rejection) for unknown --cluster, "
            f"got {result.returncode}"
        )

    def test_selector_deleted_cluster_ipc_exits_nonzero(self) -> None:
        """--cluster ipc exits 2 — ipc cluster was deleted under DR-215/C5.

        No ipc-cluster probes exist in the manifest post-C5.  The probe script
        rejects unknown cluster names rather than returning an empty list.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "ipc")

        # exit 2 = argparse/manifest rejection before any probe runs.
        assert result.returncode == 2, (
            f"Expected exit 2 (argparse/manifest rejection) for deleted --cluster ipc "
            f"(no ipc cluster in manifest post-C5), got {result.returncode}"
        )

    def test_selector_deleted_cluster_coordinator_exits_nonzero(self) -> None:
        """--cluster coordinator exits 2 — coordinator cluster was deleted under DR-215/C5."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "coordinator")

        # exit 2 = argparse/manifest rejection before any probe runs.
        assert result.returncode == 2, (
            f"Expected exit 2 (argparse/manifest rejection) for deleted --cluster coordinator "
            f"(no coordinator cluster in manifest post-C5), got {result.returncode}"
        )

    def test_selector_triage_and_step_zero_together_exits_nonzero(self) -> None:
        """--triage --step-zero together exits nonzero (argparse mutual exclusion)."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--triage", "--step-zero")

        assert result.returncode != 0, (
            "Expected non-zero exit when --triage and --step-zero are combined (mutually exclusive)"
        )

    def test_selector_step_zero_emits_one_ndjson_line_per_probe(self) -> None:
        """--step-zero emits exactly one NDJSON line per manifest probe.

        Exit 0 or 1 are both valid (depends on whether required probes pass);
        this test only verifies the output shape and line count.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--step-zero")

        # Exit 0 (all pass) and exit 1 (required probe failed) are both valid.
        # Exit 2 (argparse / manifest error) is not valid.
        assert result.returncode in {0, 1}, (
            f"--step-zero must exit 0 or 1, got {result.returncode}; "
            f"stderr: {result.stderr[:300]}"
        )

        lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        assert len(lines) == len(_IMPLEMENTED_IDS), (
            f"--step-zero must emit exactly one NDJSON line per manifest probe "
            f"({len(_IMPLEMENTED_IDS)}), got {len(lines)}:\n"
            + "\n".join(lines[: len(_IMPLEMENTED_IDS) + 1])
        )

        # Each line must parse as JSON and have the five required keys.
        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f"Line {i} is not valid JSON: {exc!r}; line: {line!r}")
            for key in ("name", "status", "severity", "detail", "remediation"):
                assert key in obj, f"Step-zero line {i} missing key {key!r}: {obj}"

    # -----------------------------------------------------------------------
    # Enriched-field tests (required / skipped / cluster in probe rows)
    # -----------------------------------------------------------------------

    def test_enriched_fields_present_in_default_output(self) -> None:
        """Default mode probe rows include required, skipped, cluster fields."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run()

        assert result.returncode == 0, (
            f"Default mode exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        for row in envelope["probes"]:
            pid = row.get("probe", "<unknown>")
            assert "required" in row, f"Probe {pid!r} missing 'required' field"
            assert "skipped" in row, f"Probe {pid!r} missing 'skipped' field"
            assert "cluster" in row, f"Probe {pid!r} missing 'cluster' field"

    def test_enriched_cluster_field_matches_manifest(self) -> None:
        """Probe rows carry the correct cluster value from doctor-probes.toml."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run()

        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        probe_map = {p["probe"]: p for p in envelope["probes"]}

        # Spot-check known cluster assignments from doctor-probes.toml post-C5.
        expected_clusters = {
            "claude-klabauter.root.resolve":    "registry",
            "claude-klabauter.registry.key":    "registry",
            "claude-klabauter.core.import":     "install",
            "claude-klabauter.resident.debris": "install",
            "claude-klabauter.version.sanity":  "install",
            "claude-klabauter.invoke.smoke":    "dispatch",
        }
        for pid, expected_cluster in expected_clusters.items():
            if pid in probe_map:
                actual = probe_map[pid].get("cluster")
                assert actual == expected_cluster, (
                    f"Probe {pid!r} cluster: expected {expected_cluster!r}, got {actual!r}"
                )

    # -----------------------------------------------------------------------
    # Per-probe --probe selector tests for C1b probes
    # -----------------------------------------------------------------------

    def test_resident_debris_probe_selector(self) -> None:
        """--probe claude-klabauter.resident.debris returns exactly that one probe."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.resident.debris")

        assert result.returncode == 0, (
            f"--probe claude-klabauter.resident.debris exited {result.returncode}; "
            f"stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = [p["probe"] for p in envelope["probes"]]
        assert len(probe_ids) == 1, (
            f"Expected exactly 1 probe, got {probe_ids}"
        )
        assert probe_ids[0] == "claude-klabauter.resident.debris"

    def test_resident_debris_in_triage(self) -> None:
        """claude-klabauter.resident.debris is triage=true — must appear in --triage output."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--triage")

        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}
        assert "claude-klabauter.resident.debris" in probe_ids, (
            "claude-klabauter.resident.debris has triage=true and must appear in --triage output"
        )

    def test_resident_debris_status_never_degraded_on_healthy_repo(self) -> None:
        """claude-klabauter.resident.debris emits PASS or INFO in default output, NEVER DEGRADED.

        On this development repo (CLAUDE_KLABAUTER_ROOT set to repo root), the probe either
        finds no debris (→ PASS) or finds stale debris (→ INFO).  It must never
        emit DEGRADED — debris is harmless-but-stale per DR-215 negative-spec.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run()

        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        probe_map = {p["probe"]: p for p in envelope["probes"]}

        if "claude-klabauter.resident.debris" not in probe_map:
            pytest.skip("claude-klabauter.resident.debris not in default output")

        status = probe_map["claude-klabauter.resident.debris"]["status"]
        assert status in {"PASS", "INFO"}, (
            f"claude-klabauter.resident.debris emitted {status!r}; must be PASS or INFO, never DEGRADED. "
            "Debris is harmless-but-stale post-DR-215 (no running process attaches to it)."
        )
        assert status != "DEGRADED", (
            "claude-klabauter.resident.debris must NEVER emit DEGRADED — "
            "debris is an INFO advisory, not a genuine hard failure"
        )

    def test_version_sanity_probe_selector(self) -> None:
        """--probe claude-klabauter.version.sanity returns exactly that one probe."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.version.sanity")

        assert result.returncode == 0, (
            f"--probe claude-klabauter.version.sanity exited {result.returncode}; "
            f"stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = [p["probe"] for p in envelope["probes"]]
        assert len(probe_ids) == 1, (
            f"Expected exactly 1 probe, got {probe_ids}"
        )
        assert probe_ids[0] == "claude-klabauter.version.sanity"

    def test_version_sanity_in_triage(self) -> None:
        """claude-klabauter.version.sanity is triage=true — must appear in --triage output."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--triage")

        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}
        assert "claude-klabauter.version.sanity" in probe_ids, (
            "claude-klabauter.version.sanity has triage=true and must appear in --triage output"
        )

    def test_invoke_smoke_probe_selector(self) -> None:
        """--probe claude-klabauter.invoke.smoke returns exactly that one probe."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.invoke.smoke")

        assert result.returncode == 0, (
            f"--probe claude-klabauter.invoke.smoke exited {result.returncode}; "
            f"stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = [p["probe"] for p in envelope["probes"]]
        assert len(probe_ids) == 1, (
            f"Expected exactly 1 probe, got {probe_ids}"
        )
        assert probe_ids[0] == "claude-klabauter.invoke.smoke"

    def test_invoke_smoke_is_optional(self) -> None:
        """claude-klabauter.invoke.smoke carries required=False (OPTIONAL probe)."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run()

        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        probe_map = {p["probe"]: p for p in envelope["probes"]}

        if "claude-klabauter.invoke.smoke" not in probe_map:
            pytest.skip("claude-klabauter.invoke.smoke not in default output")

        assert probe_map["claude-klabauter.invoke.smoke"]["required"] is False, (
            "claude-klabauter.invoke.smoke must be required=False (OPTIONAL probe — "
            "a purely-static run must not be held to BROKEN on this probe alone)"
        )

    def test_invoke_smoke_in_triage(self) -> None:
        """claude-klabauter.invoke.smoke is triage=true — must appear in --triage output."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--triage")

        assert result.returncode == 0
        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}
        assert "claude-klabauter.invoke.smoke" in probe_ids, (
            "claude-klabauter.invoke.smoke has triage=true and must appear in --triage output"
        )

    def test_invoke_smoke_result_emits_parseable_envelope(self) -> None:
        """claude-klabauter.invoke.smoke emits a parseable result envelope (PASS, BROKEN, or SKIP).

        Probe-authoring invariant: the probe must ALWAYS emit a parseable
        _ProbeResult, never a bare crash, even on spawn failure.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.invoke.smoke")

        # Exit 0 or 1 are valid (probe outcome varies by environment).
        # Exit 2 would indicate a manifest validation error — not expected here.
        assert result.returncode in {0, 1}, (
            f"--probe claude-klabauter.invoke.smoke must exit 0 or 1, got {result.returncode}; "
            f"stderr: {result.stderr[:300]}"
        )

        envelope = json.loads(result.stdout)
        assert envelope["schema_version"] == 1, "Envelope must carry schema_version=1"
        probes = envelope.get("probes", [])
        assert len(probes) == 1, f"Expected 1 probe row, got {len(probes)}"
        row = probes[0]
        assert row["probe"] == "claude-klabauter.invoke.smoke"
        assert row["status"] in {"PASS", "BROKEN", "INFO", "SKIP"}, (
            f"invoke.smoke status must be one of PASS/BROKEN/INFO/SKIP, got {row['status']!r}; "
            "it must NEVER produce a bare crash"
        )

    # -----------------------------------------------------------------------
    # Deleted probe id guard tests
    # -----------------------------------------------------------------------

    def test_deleted_probe_shim_handshake_exits_nonzero(self) -> None:
        """--probe claude-klabauter.shim.handshake exits nonzero — probe deleted under DR-215."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.shim.handshake")

        # exit 2 = argparse/manifest rejection before any probe runs.
        assert result.returncode == 2, (
            f"claude-klabauter.shim.handshake was deleted under DR-215; "
            f"expected exit 2 (argparse/manifest rejection), got {result.returncode}"
        )

    def test_deleted_probe_shim_harness_env_exits_nonzero(self) -> None:
        """--probe claude-klabauter.shim.harness_env exits nonzero — probe deleted under DR-215."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.shim.harness_env")

        # exit 2 = argparse/manifest rejection before any probe runs.
        assert result.returncode == 2, (
            f"claude-klabauter.shim.harness_env was deleted under DR-215; "
            f"expected exit 2 (argparse/manifest rejection), got {result.returncode}"
        )

    def test_deleted_probe_uds_ping_exits_nonzero(self) -> None:
        """--probe claude-klabauter.core.uds.ping exits nonzero — probe deleted under DR-215."""
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--probe", "claude-klabauter.core.uds.ping")

        # exit 2 = argparse/manifest rejection before any probe runs.
        assert result.returncode == 2, (
            f"claude-klabauter.core.uds.ping was deleted under DR-215; "
            f"expected exit 2 (argparse/manifest rejection), got {result.returncode}"
        )


# ---------------------------------------------------------------------------
# Helpers shared by the Python-version gate tests
# ---------------------------------------------------------------------------


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Returns the module, or None if loading fails (caller must pytest.skip).
    Each call produces a fresh module instance — safe to monkeypatch in isolation.

    The module is registered in sys.modules under a unique key before exec so
    that Python's dataclass annotation-resolution path (sys.modules[cls.__module__])
    finds a valid namespace on Python 3.14+.
    """
    if not _BIN_PROBE.exists():
        return None
    _MODULE_KEY = "claude_klabauter_doctor_probe_unit"
    spec = importlib.util.spec_from_file_location(_MODULE_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module so dataclass __module__ lookups succeed.
    sys.modules[_MODULE_KEY] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_MODULE_KEY, None)
        return None
    return mod


# ---------------------------------------------------------------------------
# Python-version gate unit tests (tomllib-absent path)
# ---------------------------------------------------------------------------


class TestPythonVersionBrokenEnvelope:
    """Unit tests for _python_version_broken_envelope() and the tomllib-absent main() path.

    These tests load bin/claude-klabauter-doctor-probe.py as a module and operate directly
    on its internal helpers — no subprocess — so they are fast and do not
    require the full install chain to be live.

    Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C6
    """

    def test_envelope_schema_version(self) -> None:
        """_python_version_broken_envelope() returns schema_version 1."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        assert envelope["schema_version"] == 1, (
            f"Expected schema_version 1, got {envelope.get('schema_version')!r}"
        )

    def test_envelope_overall_broken(self) -> None:
        """_python_version_broken_envelope() has overall == 'BROKEN'."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        assert envelope["overall"] == "BROKEN", (
            f"Expected overall 'BROKEN', got {envelope.get('overall')!r}"
        )

    def test_envelope_status_vocab_present(self) -> None:
        """_python_version_broken_envelope() includes status_vocab with BROKEN."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        assert "status_vocab" in envelope
        assert "BROKEN" in envelope["status_vocab"]

    def test_envelope_single_probe_row(self) -> None:
        """_python_version_broken_envelope() probes list has exactly one entry."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        assert isinstance(envelope["probes"], list)
        assert len(envelope["probes"]) == 1, (
            f"Expected 1 probe row, got {len(envelope['probes'])}"
        )

    def test_envelope_probe_id_is_python_version(self) -> None:
        """_python_version_broken_envelope() probe id is 'claude-klabauter.python.version'."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        probe = envelope["probes"][0]
        assert probe["probe"] == "claude-klabauter.python.version", (
            f"Expected probe id 'claude-klabauter.python.version', got {probe.get('probe')!r}"
        )

    def test_envelope_probe_status_broken(self) -> None:
        """_python_version_broken_envelope() probe row has status 'BROKEN'."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        probe = envelope["probes"][0]
        assert probe["status"] == "BROKEN", (
            f"Expected probe status 'BROKEN', got {probe.get('status')!r}"
        )

    def test_envelope_probe_required_and_not_skipped(self) -> None:
        """_python_version_broken_envelope() probe has required=True, skipped=False."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        probe = envelope["probes"][0]
        assert probe["required"] is True, f"Expected required=True, got {probe.get('required')!r}"
        assert probe["skipped"] is False, f"Expected skipped=False, got {probe.get('skipped')!r}"

    def test_envelope_probe_cluster_install(self) -> None:
        """_python_version_broken_envelope() probe has cluster='install'."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        probe = envelope["probes"][0]
        assert probe["cluster"] == "install", (
            f"Expected cluster 'install', got {probe.get('cluster')!r}"
        )

    def test_envelope_probe_remediation_nonempty(self) -> None:
        """_python_version_broken_envelope() probe has a non-empty remediation string."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        probe = envelope["probes"][0]
        assert isinstance(probe["remediation"], str) and len(probe["remediation"]) > 0, (
            f"Expected non-empty remediation string, got {probe.get('remediation')!r}"
        )

    def test_envelope_is_valid_json_serialisable(self) -> None:
        """_python_version_broken_envelope() round-trips through json.dumps/loads cleanly."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        envelope = mod._python_version_broken_envelope()
        serialised = json.dumps(envelope, indent=2, default=str)
        parsed = json.loads(serialised)
        assert parsed["schema_version"] == 1
        assert parsed["overall"] == "BROKEN"

    def test_main_returns_1_when_tomllib_unavailable(self) -> None:
        """main() returns 1 and emits a parseable BROKEN envelope when _TOMLLIB_AVAILABLE is False."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        # Patch the module-level flag AND the module attribute to faithfully simulate
        # the actual Python < 3.11 condition where both the flag is False and the
        # tomllib name is None.
        mod._TOMLLIB_AVAILABLE = False
        mod.tomllib = None

        # Patch sys.argv so argparse sees no flags (default envelope mode).
        old_argv = sys.argv
        old_stdout = sys.stdout
        sys.argv = ["claude-klabauter-doctor-probe"]
        sys.stdout = io.StringIO()
        try:
            result = mod.main()
        except SystemExit as exc:
            result = exc.code
        finally:
            captured = sys.stdout.getvalue()
            sys.stdout = old_stdout
            sys.argv = old_argv

        assert result == 1, f"Expected exit code 1 when tomllib unavailable, got {result!r}"
        assert captured.strip(), "Expected non-empty stdout when tomllib unavailable"

        envelope = json.loads(captured.strip())
        assert envelope["schema_version"] == 1
        assert envelope["overall"] == "BROKEN", (
            f"Expected envelope overall='BROKEN', got {envelope.get('overall')!r}"
        )
        assert envelope["probes"][0]["probe"] == "claude-klabauter.python.version"

    def test_main_step_zero_returns_1_when_tomllib_unavailable(self) -> None:
        """main() with --step-zero returns 1 and emits one valid NDJSON line when tomllib absent."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        mod._TOMLLIB_AVAILABLE = False
        # Review: code-reviewer — F5: also patch mod.tomllib to match the non-step-zero
        # variant; without this, real tomllib is still bound and step-zero code that calls
        # tomllib.loads() directly can succeed even though _TOMLLIB_AVAILABLE is False.
        mod.tomllib = None

        old_argv = sys.argv
        old_stdout = sys.stdout
        sys.argv = ["claude-klabauter-doctor-probe", "--step-zero"]
        sys.stdout = io.StringIO()
        try:
            result = mod.main()
        except SystemExit as exc:
            result = exc.code
        finally:
            captured = sys.stdout.getvalue()
            sys.stdout = old_stdout
            sys.argv = old_argv

        assert result == 1, f"Expected exit code 1 in --step-zero tomllib-absent path, got {result!r}"
        lines = [ln for ln in captured.strip().splitlines() if ln.strip()]
        assert len(lines) == 1, f"Expected exactly 1 NDJSON line, got {len(lines)}: {lines}"

        obj = json.loads(lines[0])
        for key in ("name", "status", "severity", "detail", "remediation"):
            assert key in obj, f"Step-zero NDJSON line missing key {key!r}: {obj}"
        assert obj["name"] == "claude-klabauter.python.version"
        assert obj["status"] == "fail"
        assert obj["severity"] == "hard"


# ---------------------------------------------------------------------------
# C9: claude-klabauter.warm.roundtrip opt-in-flag unit tests
#
# Direct-import (_load_probe_module), no subprocess — fast tier per the chunk
# brief's PREFER-a-stubbed-transport guidance. Stubs
# coordinator_core.warm.client.try_warm_dispatch so a call proves an attempted
# connection without needing a real warm server; the default-off assertion
# relies on that stub NEVER being invoked.
# ---------------------------------------------------------------------------


class TestWarmRoundtripOptInFlag:
    """claude-klabauter.warm.roundtrip defaults off; --include-live-roundtrip is the only opt-in.

    Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C9.
    """

    def test_flag_defaults_off_and_no_connection_attempted(self) -> None:
        """_run_probe_warm_roundtrip(claude_klabauter_root, False) never calls try_warm_dispatch."""
        mod = _load_probe_module()
        if mod is None:
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")

        called = {"n": 0}

        class _FakeWarmClient:
            @staticmethod
            def try_warm_dispatch(msg):
                called["n"] += 1
                return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

        # Prevent the real coordinator_core.warm.client from ever being imported
        # inside the probe — if the probe attempted a connection despite the flag
        # being False, this stub would be the thing it called, and `called["n"]`
        # would move off zero.
        sys.modules["coordinator_core.warm.client"] = _FakeWarmClient()  # type: ignore[assignment]
        try:
            result = mod._run_probe_warm_roundtrip(_REPO_ROOT, False)
        finally:
            sys.modules.pop("coordinator_core.warm.client", None)

        assert called["n"] == 0, (
            "claude-klabauter.warm.roundtrip attempted a connection with include_live_roundtrip=False"
        )
        assert result.probe == "claude-klabauter.warm.roundtrip"
        assert result.skipped is True, (
            "Flag-absent path must report skipped=True (not an INFO stub) — "
            "see the probe's own docstring."
        )
        assert result.required is False

    def test_cluster_warm_without_flag_still_returns_a_skipped_row(self) -> None:
        """--cluster warm (no --include-live-roundtrip) still returns claude-klabauter.warm.roundtrip.

        Confirms _apply_selector does not drop the probe's row from --cluster warm
        membership just because it is skipped — the manifest-declared id must still
        surface, per the chunk brief.
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run("--cluster", "warm")

        assert result.returncode == 0, (
            f"--cluster warm exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_map = {p["probe"]: p for p in envelope["probes"]}

        assert "claude-klabauter.warm.roundtrip" in probe_map, (
            "claude-klabauter.warm.roundtrip must appear in --cluster warm output even without "
            "--include-live-roundtrip"
        )
        row = probe_map["claude-klabauter.warm.roundtrip"]
        assert row.get("skipped") is True, (
            f"claude-klabauter.warm.roundtrip without --include-live-roundtrip must report "
            f"skipped=True, got {row.get('skipped')!r}"
        )

    def test_selector_default_returns_every_manifest_probe_reconfirmed(self) -> None:
        """AC6 re-check: registration of claude-klabauter.warm.roundtrip does not break the
        default-mode roster invariant (test_selector_default_returns_every_manifest_probe).
        """
        if not _BIN_PROBE.exists():
            pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk")

        result = _run()

        assert result.returncode == 0, (
            f"Default mode exited {result.returncode}; stderr: {result.stderr[:400]}"
        )

        envelope = json.loads(result.stdout)
        probe_ids = {p["probe"] for p in envelope["probes"]}

        assert "claude-klabauter.warm.roundtrip" in _IMPLEMENTED_IDS, (
            "claude-klabauter.warm.roundtrip must be declared in bin/doctor-probes.toml"
        )
        assert probe_ids == _IMPLEMENTED_IDS, (
            f"Probe set mismatch in default output: "
            f"missing={_IMPLEMENTED_IDS - probe_ids!r}, "
            f"unexpected={probe_ids - _IMPLEMENTED_IDS!r}"
        )
