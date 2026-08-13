"""
bin.tests.test_claude_klabauter_doctor_schema_drift_probe — unit tests for the
Claude-klabauter.schema.vendor_drift doctor probe.

Covers the probe's verdict mapping over the four statuses
coordinator_core.frontmatter.schema_drift_watch can return, by monkeypatching that
module's scan function (the scan itself is tested exhaustively in
coordinator_core/frontmatter/tests/test_schema_drift_watch.py — this file tests the
WIRING: status -> _ProbeResult status/required/skipped).

  MATCH          -> PASS
  DRIFT          -> DEGRADED (never BROKEN — advisory nudge, not a broken install)
  INDETERMINATE  -> DEGRADED worded as indeterminate (never PASS, never a drift claim)
  UNRESOLVED     -> SKIP-as-advisory (no coordinator-claude clone on this machine)

Probe-authoring invariant (per state/lessons/2026-07-04-a-diagnostic-must-always-emit-a-parseabl.yaml):
  Every probe must emit a parseable _ProbeResult on ALL paths — including its own
  bootstrap failure. Asserted explicitly on every fault path below, including a
  scan function that raises.

Non-gating invariant: required=False on every path, so --step-zero (whose exit code
keys off REQUIRED probes) can never fail because of vendored-schema drift.

Spec backlink: coordinator_core/frontmatter/schema_drift_watch.py module docstring.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Registered in sys.modules under a unique key BEFORE exec so Python's dataclass
    annotation-resolution path (sys.modules[cls.__module__]) finds a valid namespace
    on Python 3.14+.
    """
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_schema_drift_unit"
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


def _is_parseable_probe_result(r: object) -> bool:
    """Return True iff r is a _ProbeResult with the required fields populated."""
    return (
        hasattr(r, "probe")
        and hasattr(r, "status")
        and hasattr(r, "detail")
        and hasattr(r, "remediation")
        and isinstance(r.probe, str) and len(r.probe) > 0  # type: ignore[union-attr]
        and isinstance(r.status, str) and len(r.status) > 0  # type: ignore[union-attr]
    )


def _patch_scan(monkeypatch: pytest.MonkeyPatch, report) -> None:
    """Replace the watch module's scan with a stub returning `report` (or raising it)."""
    import coordinator_core.frontmatter.schema_drift_watch as watch

    def _stub(*_args, **_kwargs):
        if isinstance(report, BaseException):
            raise report
        return report

    monkeypatch.setattr(watch, "scan_vendored_schema_drift", _stub)


def _report(status: str, **extra):
    base = {
        "status": status,
        "doe_repo_path": "/fake/coordinator-claude",
        "checked": 12,
        "matched": [],
        "drifted": [],
        "indeterminate": [],
        "summary": f"stub summary for {status}",
    }
    base.update(extra)
    return base


class TestVendoredSchemaDriftProbe:
    """Verdict mapping for claude-klabauter.schema.vendor_drift."""

    def test_match_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()
        _patch_scan(monkeypatch, _report("MATCH", matched=["a.schema.json"]))

        result = mod._run_probe_vendored_schema_drift(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.schema.vendor_drift"
        assert result.status == mod._PASS
        assert result.required is False
        assert result.skipped is False

    def test_drift_is_degraded_never_broken(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()
        _patch_scan(
            monkeypatch,
            _report(
                "DRIFT",
                drifted=[{
                    "schema": "improvement-queue.schema.json",
                    "detail": "diverges",
                    "direction": "we-are-behind",
                    "local_version": "1.0.0",
                    "doe_version": "1.1.0",
                }],
                summary=(
                    "1/12 vendored schema(s) diverge from coordinator-claude HEAD: "
                    "improvement-queue.schema.json [we-are-behind]"
                ),
            ),
        )

        result = mod._run_probe_vendored_schema_drift(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.status != mod._BROKEN
        assert result.required is False, "drift must never fail --step-zero"
        assert result.skipped is False
        assert "improvement-queue.schema.json" in result.detail
        assert "re-vendor" in result.remediation.lower()
        assert result.data["drifted"] == [
            {
                "schema": "improvement-queue.schema.json",
                "direction": "we-are-behind",
                "local_version": "1.0.0",
                "doe_version": "1.1.0",
            }
        ], "the operator-facing data must carry direction and both x-schema-version reads"

    def test_indeterminate_is_degraded_not_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unreadable coordinator-claude clone: visibly indeterminate — never silent green, never drift."""
        mod = _require_module()
        _patch_scan(
            monkeypatch,
            _report(
                "INDETERMINATE",
                indeterminate=[{"schema": "handoff.schema.json", "detail": "cannot read"}],
                summary="INDETERMINATE — could not compare 1/12 vendored schema(s); the check did not run.",
            ),
        )

        result = mod._run_probe_vendored_schema_drift(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.status != mod._PASS
        assert result.required is False
        assert result.skipped is False
        assert "INDETERMINATE" in result.detail
        assert "UNKNOWN, not clean" in result.remediation
        assert result.data["drifted"] == [], "indeterminate must not be reported as drift"

    def test_unresolved_doe_clone_is_skip_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No coordinator-claude clone at all (fresh machine / CI): SKIP-as-advisory, graceful."""
        mod = _require_module()
        _patch_scan(
            monkeypatch,
            _report("UNRESOLVED", doe_repo_path=None, checked=0,
                    summary="No coordinator-claude clone resolved on this machine"),
        )

        result = mod._run_probe_vendored_schema_drift(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.skipped is True
        assert result.required is False
        assert result.status == mod._INFO

    def test_claude_klabauter_root_none_is_skip(self) -> None:
        mod = _require_module()

        result = mod._run_probe_vendored_schema_drift(None)

        assert _is_parseable_probe_result(result)
        assert result.skipped is True
        assert result.required is False

    def test_scan_raising_still_yields_parseable_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashing scan must degrade to SKIP, not propagate — the doctor never crashes."""
        mod = _require_module()
        _patch_scan(monkeypatch, RuntimeError("boom"))

        result = mod._run_probe_vendored_schema_drift(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.skipped is True
        assert result.required is False
        assert "boom" in result.detail

    def test_live_run_against_real_tree_never_raises(self) -> None:
        """Unpatched, end-to-end against this machine's real tree: always a parseable result."""
        mod = _require_module()

        result = mod._run_probe_vendored_schema_drift(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.schema.vendor_drift"
        assert result.required is False
        assert result.status != mod._BROKEN


class TestManifestRegistration:
    """The probe must be declared in bin/doctor-probes.toml, and in the triage set."""

    def test_probe_is_registered_and_triage_true(self) -> None:
        import tomllib

        data = tomllib.loads((_REPO_ROOT / "bin" / "doctor-probes.toml").read_text(encoding="utf-8"))
        entries = [p for p in data["probe"] if p["id"] == "claude-klabauter.schema.vendor_drift"]

        assert len(entries) == 1, "claude-klabauter.schema.vendor_drift must be declared exactly once"
        entry = entries[0]
        assert entry["triage"] is True, (
            "must be triage=true — only --triage/full runs write state/doctor-last-run.json, "
            "the sentinel /workday-start reads; triage=false would never reach cadence"
        )
        assert entry["required"] is False, "must be optional — drift is advisory, not a broken install"
        assert entry["cluster"] == "install"
        assert entry["weight"] in {"cheap", "standard"}, "triage invariant: never heavy"
        assert entry["body"] == "bin/claude-klabauter-doctor-probe.py:_run_probe_vendored_schema_drift"
