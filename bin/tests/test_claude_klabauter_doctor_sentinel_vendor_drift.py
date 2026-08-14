"""
bin.tests.test_claude_klabauter_doctor_sentinel_vendor_drift — unit tests for the additive
`vendor_drift` key on state/doctor-last-run.json.

Covers:
  - `_sentinel_vendor_drift` reduces the claude-klabauter.schema.vendor_drift probe row's
    `data` into the documented public shape.
  - The absent-row case (probe not present in this run's envelope, e.g. a scalpel
    `--probe`/`--cluster` run for something else) yields status="UNKNOWN" with
    empty lists — never indistinguishable from "checked, clean".
  - `_write_doctor_sentinel` writes `vendor_drift` alongside the untouched original
    7 keys, without altering their shape.

Spec backlink: cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md
Sibling coverage: bin/tests/test_claude_klabauter_doctor_schema_drift_probe.py (probe-level
wiring of local_version/doe_version into `data["drifted"]`, tested upstream of
this file's sentinel-reduction layer).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Mirrors bin/tests/test_claude_klabauter_doctor_schema_drift_probe.py's loader — see that
    file's docstring for why the module is registered in sys.modules before exec.
    """
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_sentinel_vendor_drift_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(key, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


def _envelope(probes: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "status_vocab": ["BROKEN", "DEGRADED", "INFO", "PASS"],
        "overall": "PASS",
        "probes": probes,
        "warnings": [],
        "missing_optional": [],
    }


class TestSentinelVendorDriftReduction:
    """_sentinel_vendor_drift's reduction of the probe row's `data` dict."""

    def test_drift_row_reduces_to_documented_shape(self) -> None:
        mod = _require_module()
        envelope = _envelope([
            {
                "probe": "claude-klabauter.schema.vendor_drift",
                "status": "DEGRADED",
                "detail": "1/12 diverge",
                "remediation": "re-vendor",
                "data": {
                    "status": "DRIFT",
                    "doe_repo_path": "/fake/DoE-claude",
                    "checked": 12,
                    "drifted": [
                        {
                            "schema": "improvement-queue.schema.json",
                            "direction": "we-are-behind",
                            "local_version": "1.0.0",
                            "doe_version": "1.1.0",
                        }
                    ],
                    "indeterminate": [],
                },
            }
        ])

        result = mod._sentinel_vendor_drift(envelope)

        assert result == {
            "status": "DRIFT",
            "checked": 12,
            "drifted": [
                {
                    "schema": "improvement-queue.schema.json",
                    "direction": "we-are-behind",
                    "local_version": "1.0.0",
                    "doe_version": "1.1.0",
                }
            ],
            "indeterminate": [],
        }

    def test_match_row_reduces_to_clean_shape(self) -> None:
        mod = _require_module()
        envelope = _envelope([
            {
                "probe": "claude-klabauter.schema.vendor_drift",
                "status": "PASS",
                "detail": "all match",
                "remediation": "—",
                "data": {
                    "status": "MATCH",
                    "doe_repo_path": "/fake/DoE-claude",
                    "checked": 12,
                    "drifted": [],
                    "indeterminate": [],
                },
            }
        ])

        result = mod._sentinel_vendor_drift(envelope)

        assert result["status"] == "MATCH"
        assert result["checked"] == 12
        assert result["drifted"] == []
        assert result["indeterminate"] == []

    def test_absent_probe_row_yields_unknown_not_clean(self) -> None:
        """The probe didn't run in this selection — absent must not read as clean."""
        mod = _require_module()
        envelope = _envelope([
            {"probe": "claude-klabauter.some.other.probe", "status": "PASS", "detail": "", "remediation": "", "data": {}},
        ])

        result = mod._sentinel_vendor_drift(envelope)

        assert result == {"status": "UNKNOWN", "checked": None, "drifted": [], "indeterminate": []}

    def test_no_probes_at_all_yields_unknown(self) -> None:
        mod = _require_module()
        envelope = _envelope([])

        result = mod._sentinel_vendor_drift(envelope)

        assert result["status"] == "UNKNOWN"
        assert result["drifted"] == []
        assert result["indeterminate"] == []

    def test_malformed_envelope_never_raises(self) -> None:
        mod = _require_module()
        result = mod._sentinel_vendor_drift({"probes": "not-a-list"})
        assert result == {"status": "UNKNOWN", "checked": None, "drifted": [], "indeterminate": []}


class TestWriteDoctorSentinelVendorDriftKey:
    """_write_doctor_sentinel writes `vendor_drift` alongside the untouched 7 keys."""

    def test_sentinel_carries_vendor_drift_and_original_seven_keys(
        self, tmp_path: Path
    ) -> None:
        mod = _require_module()
        envelope = _envelope([
            {
                "probe": "claude-klabauter.schema.vendor_drift",
                "status": "DEGRADED",
                "detail": "1/12 diverge",
                "remediation": "re-vendor",
                "data": {
                    "status": "DRIFT",
                    "doe_repo_path": "/fake/DoE-claude",
                    "checked": 12,
                    "drifted": [
                        {
                            "schema": "improvement-queue.schema.json",
                            "direction": "we-are-behind",
                            "local_version": "1.0.0",
                            "doe_version": "1.1.0",
                        }
                    ],
                    "indeterminate": [],
                },
            }
        ])
        envelope["overall"] = "DEGRADED"

        mod._write_doctor_sentinel(envelope, tmp_path)

        sentinel_path = tmp_path / "state" / "doctor-last-run.json"
        assert sentinel_path.is_file()
        written = json.loads(sentinel_path.read_text(encoding="utf-8"))

        # Original 7 keys, unchanged shape.
        for key in ("ran_at", "ts", "verdict", "red_probes", "hint", "schema_version", "plugin"):
            assert key in written
        assert written["schema_version"] == 1
        assert written["plugin"] == "claude-klabauter"
        assert written["verdict"] == "AMBER"

        # Additive 8th key.
        assert written["vendor_drift"]["status"] == "DRIFT"
        assert written["vendor_drift"]["drifted"][0]["local_version"] == "1.0.0"
        assert written["vendor_drift"]["drifted"][0]["doe_version"] == "1.1.0"

    def test_sentinel_vendor_drift_present_even_when_probe_row_absent(
        self, tmp_path: Path
    ) -> None:
        """A --probe/--cluster scalpel envelope that never ran the drift probe still
        gets a well-formed vendor_drift key — UNKNOWN, not missing."""
        mod = _require_module()
        envelope = _envelope([
            {"probe": "claude-klabauter.some.other.probe", "status": "PASS", "detail": "", "remediation": "", "data": {}},
        ])

        mod._write_doctor_sentinel(envelope, tmp_path)

        written = json.loads((tmp_path / "state" / "doctor-last-run.json").read_text(encoding="utf-8"))
        assert written["vendor_drift"] == {
            "status": "UNKNOWN", "checked": None, "drifted": [], "indeterminate": []
        }
