"""
bin.tests.test_claude_klabauter_doctor_route_share_probe — Unit tests for
Claude-klabauter.warm.route_share (C6).

Covers `_run_probe_warm_route_share` — TRANSLATES
`coordinator_core.telemetry.engine_report.route_distribution`'s verdict into
the closed probe-status enum, without re-parsing the op-latency sink itself.

Loads bin/claude-klabauter-doctor-probe.py as a module via importlib, matching the
existing loader pattern in test_claude_klabauter_doctor_generation_probe.py — own
module key, so this file's module instance never collides with a sibling
test file's in sys.modules.

Covered:
  AC5  — "cannot tell" at today's coverage: skipped=True + required=True,
         never PASS and never FAIL.
  AC5b — this probe never uses `_INFO`; and the emitted envelope's `overall`
         field (via `_build_envelope_via_module`, never
         `_local_reduce_overall` called directly) is DEGRADED, never PASS,
         for an otherwise-all-PASS probe set that includes this probe at
         today's coverage.
  AC13 — the reader's own "ok"/"degraded"/"unknown" verdict lands in
         `data["reader_verdict"]`, never in `_ProbeResult.status`.
  AC6  — re-run of test_selector_default_returns_every_manifest_probe lives
         in coordinator_core/tests/test_claude_klabauter_doctor_probe_selectors.py,
         not here — the manifest addition self-registers via that test's
         own `_IMPLEMENTED_IDS` derivation.

Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C6.
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
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib."""
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_route_share_probe_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_KEY] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_KEY, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    try:
        import coordinator_core  # noqa: F401
        import coordinator_core.telemetry.engine_report  # noqa: F401
    except ImportError:
        pytest.skip("coordinator_core.telemetry.engine_report not importable in this environment")
    return mod  # type: ignore[return-value]


def _is_parseable_probe_result(r: object) -> bool:
    return (
        hasattr(r, "probe")
        and hasattr(r, "status")
        and hasattr(r, "detail")
        and hasattr(r, "remediation")
        and isinstance(r.probe, str) and len(r.probe) > 0  # type: ignore[union-attr]
        and isinstance(r.status, str) and len(r.status) > 0  # type: ignore[union-attr]
    )


class TestWarmRouteShareProbe:
    """_run_probe_warm_route_share() — AC5, AC5b, AC13."""

    def test_claude_klabauter_root_none_never_uses_info(self) -> None:
        mod = _require_module()

        result = mod._run_probe_warm_route_share(None)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._INFO
        assert result.skipped is True
        assert result.required is True

    def test_unknown_verdict_at_low_coverage_is_skipped_never_pass_never_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5/AC13 — real today's-coverage-shaped corpus (no rotated sink files
        on tmp_path, so iter_sink_entries yields nothing and route_distribution's
        verdict is "unknown")."""
        mod = _require_module()

        result = mod._run_probe_warm_route_share(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._INFO
        assert result.status != mod._PASS
        assert result.skipped is True
        assert result.required is True
        assert result.data is not None
        assert result.data["reader_verdict"] == "unknown"

    def test_ok_verdict_is_pass_and_reader_verdict_never_in_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC13 — reader's "ok" verdict lands in data, not in status."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        monkeypatch.setattr(
            engine_report,
            "route_distribution",
            lambda entries, **kw: {
                "complete": 100,
                "routed": 100,
                "unstamped": 0,
                "coverage": 1.0,
                "by_route": {"warm_server": 100},
                "warm_share_of_routed": 1.0,
                "verdict": "ok",
                "verdict_reason": "coverage 100% meets the floor",
            },
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._PASS
        assert result.status != "ok"
        assert result.skipped is False
        assert result.required is True
        assert result.data["reader_verdict"] == "ok"

    def test_degraded_verdict_is_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        monkeypatch.setattr(
            engine_report,
            "route_distribution",
            lambda entries, **kw: {
                "complete": 100,
                "routed": 100,
                "unstamped": 0,
                "coverage": 1.0,
                "by_route": {"warm_server": 10},
                "warm_share_of_routed": 0.1,
                "verdict": "degraded",
                "verdict_reason": "warm share below threshold",
            },
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.status != mod._INFO
        assert result.skipped is False
        assert result.required is True
        assert result.data["reader_verdict"] == "degraded"

    def test_unexpected_exception_never_uses_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        def _boom(entries, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(engine_report, "route_distribution", _boom)

        result = mod._run_probe_warm_route_share(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._INFO
        assert result.skipped is True
        assert result.required is True

    def test_envelope_overall_is_degraded_never_pass_at_todays_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5b — the emitted envelope's `overall` field, via
        `_build_envelope_via_module` (never `_local_reduce_overall` called
        directly), is DEGRADED for an otherwise-all-PASS probe set that
        includes this probe at today's (unknown-verdict) coverage."""
        mod = _require_module()

        route_share_result = mod._run_probe_warm_route_share(tmp_path)
        assert route_share_result.skipped is True
        assert route_share_result.required is True

        all_pass_probe = mod._ProbeResult(
            probe="claude-klabauter.core.import",
            status=mod._PASS,
            detail="ok",
            remediation="—",
            required=True,
        )

        envelope = mod._build_envelope_via_module(
            [all_pass_probe, route_share_result], tmp_path
        )

        assert envelope["overall"] == mod._DEGRADED, (
            f"AC5b: an otherwise-all-PASS probe set including this probe at "
            f"today's coverage must reduce to DEGRADED, got {envelope['overall']!r}"
        )
        assert envelope["overall"] != mod._PASS
