"""
bin.tests.test_claude_klabauter_doctor_route_share_probe — Unit tests for
Claude-klabauter.warm.route_share (C6, re-pinned by C2).

Covers `_run_probe_warm_route_share` — TRANSLATES
`coordinator_core.telemetry.engine_report.route_distribution`'s verdict into
the closed probe-status enum, without re-parsing the op-latency sink itself.

Loads bin/claude-klabauter-doctor-probe.py as a module via importlib, matching the
existing loader pattern in test_claude_klabauter_doctor_generation_probe.py — own
module key, so this file's module instance never collides with a sibling
test file's in sys.modules.

RE-PIN (C2, docs/plans/2026-08-19-a-windowed-coverage-refusal.md): this
fixture used to pin a SINGLE unwindowed `route_distribution` call over
whatever `iter_sink_entries` yielded, so refusal fired purely off "today's
~0.257% route coverage" (DR-328) — a function of log age, not of routing.
It now pins the EXPANDING 1h -> 6h -> 24h window (D1) and the absolute
row-count floor `_ROUTE_MIN_COMPLETE_ROWS` (D3): refusal fires only when the
widest (24h) window still holds too few complete rows, or when a window
diluted by an unstamped writer keeps coverage under the reader's floor —
never merely because the corpus is old. A fully-stamped, above-minimum
window now PASSES regardless of how old the log is.

Covered:
  AC2  — the probe reads the sink once and computes a windowed verdict over
         the expanding window, widening only while row count is under the
         minimum; the effective window is reported in `data`.
  AC3  — refusal fires only when the 24h horizon still holds too few
         complete rows, or on a window diluted by an unstamped writer; PASS
         is reachable on an above-minimum, fully-stamped window regardless
         of log age; an untimestamped, routeless row survives every window
         without sinking the verdict.
  AC4  — `data["all_time"]` is populated and differs from the windowed
         figure.
  AC5  — "cannot tell" at genuinely thin coverage: skipped=True +
         required=True, never PASS and never FAIL.
  AC5b — this probe never uses `_INFO`; and the emitted envelope's `overall`
         field (via `_build_envelope_via_module`, never
         `_local_reduce_overall` called directly) is DEGRADED, never PASS,
         for an otherwise-all-PASS probe set that includes this probe at a
         refusing shape.
  AC13 — the reader's own "ok"/"degraded"/"unknown" verdict lands in
         `data["reader_verdict"]`, never in `_ProbeResult.status`.
  AC6  — re-run of test_selector_default_returns_every_manifest_probe lives
         in coordinator_core/tests/test_claude_klabauter_doctor_probe_selectors.py,
         not here — the manifest addition self-registers via that test's
         own `_IMPLEMENTED_IDS` derivation.

Spec backlink: docs/plans/2026-08-19-a-windowed-coverage-refusal.md § C2.
"""

from __future__ import annotations

import importlib.util
import sys
import time
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
        """AC3/AC5/AC13 — empty corpus (no rotated sink files on tmp_path, so
        iter_sink_entries yields nothing): even the widest (24h) window holds
        0 complete rows, under `_ROUTE_MIN_COMPLETE_ROWS`, so the reader's
        verdict is "unknown" regardless of the (also empty) `all_time`
        figure."""
        mod = _require_module()

        result = mod._run_probe_warm_route_share(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._INFO
        assert result.status != mod._PASS
        assert result.skipped is True
        assert result.required is True
        assert result.data is not None
        assert result.data["reader_verdict"] == "unknown"
        assert result.data["effective_window_secs"] == mod._ROUTE_COVERAGE_WINDOWS_SECS[-1]

    def _make_entry(self, *, t_start, route) -> dict:
        entry: dict = {"kind": "complete"}
        if t_start is not None:
            entry["t_start"] = t_start
        if route is not None:
            entry["route"] = route
        return entry

    def test_refusal_fires_only_when_24h_horizon_still_thin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — a window whose 24h horizon holds fewer than
        `_ROUTE_MIN_COMPLETE_ROWS` complete rows refuses a verdict."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        now = time.time()
        min_rows = mod._ROUTE_MIN_COMPLETE_ROWS
        thin_entries = [
            self._make_entry(t_start=now - 100, route="warm_server")
            for _ in range(min_rows - 1)
        ]
        monkeypatch.setattr(
            engine_report,
            "iter_sink_entries",
            lambda **kw: iter(thin_entries),
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert result.data["reader_verdict"] == "unknown"
        assert result.data["effective_window_secs"] == mod._ROUTE_COVERAGE_WINDOWS_SECS[-1]
        assert result.status != mod._PASS
        assert result.skipped is True
        assert result.required is True

    def test_widens_from_1h_to_6h_to_24h_and_reports_effective_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2/AC3 — enough rows exist to clear the minimum only once the
        window widens to 6h; the reported `effective_window_secs` matches
        the horizon that actually supplied the verdict, and `data`'s
        `route_distribution` figure reflects that (not the widest) window."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        now = time.time()
        min_rows = mod._ROUTE_MIN_COMPLETE_ROWS
        # Too few rows inside 1h; enough once widened to 6h.
        entries = [
            self._make_entry(t_start=now - 1800, route="warm_server")
            for _ in range(min_rows - 10)
        ] + [
            self._make_entry(t_start=now - 7200, route="warm_server")
            for _ in range(20)
        ]
        monkeypatch.setattr(
            engine_report,
            "iter_sink_entries",
            lambda **kw: iter(entries),
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert result.data["effective_window_secs"] == 21600  # 6h
        assert result.data["route_distribution"]["complete"] == min_rows + 10
        assert result.status == mod._PASS

    def test_refusal_fires_on_window_diluted_by_unstamped_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — a window above the row-count minimum but whose rows are
        mostly unstamped (`route is None`) still refuses a verdict, because
        coverage over that window is below the reader's own floor. This is
        NOT the row-count refusal — it is the coverage-floor refusal."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        now = time.time()
        min_rows = mod._ROUTE_MIN_COMPLETE_ROWS
        # Well above the row-count minimum, but almost entirely unstamped —
        # coverage is far below the reader's floor.
        entries = [
            self._make_entry(t_start=now - 100, route=None)
            for _ in range(min_rows * 4)
        ] + [
            self._make_entry(t_start=now - 100, route="warm_server")
            for _ in range(2)
        ]
        monkeypatch.setattr(
            engine_report,
            "iter_sink_entries",
            lambda **kw: iter(entries),
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert result.data["route_distribution"]["complete"] >= min_rows
        assert result.data["reader_verdict"] == "unknown"
        assert "coverage" in result.data["route_distribution"]["verdict_reason"]
        assert result.status != mod._PASS
        assert result.skipped is True

    def test_pass_on_fully_stamped_above_minimum_window_regardless_of_log_age(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — the refusal is NOT a function of the age of the log: a
        fully-stamped window above the minimum row count PASSES even though
        every row sits near the 24h edge of the widest horizon."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        now = time.time()
        min_rows = mod._ROUTE_MIN_COMPLETE_ROWS
        old_but_fully_stamped = [
            self._make_entry(t_start=now - 80000, route="warm_server")
            for _ in range(min_rows + 5)
        ]
        monkeypatch.setattr(
            engine_report,
            "iter_sink_entries",
            lambda **kw: iter(old_but_fully_stamped),
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert result.status == mod._PASS
        assert result.data["effective_window_secs"] == mod._ROUTE_COVERAGE_WINDOWS_SECS[-1]

    def test_untimestamped_routeless_row_survives_every_window_without_sinking_verdict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 — a row with no `t_start` and no `route` is kept by
        `iter_sink_entries`'s own `since` rule (numeric-`t_start` rows only
        are filtered) at every horizon, but — being unstamped — it counts
        toward `complete`/`unstamped`, never toward `by_route`, so it cannot
        degrade an otherwise-healthy window's verdict."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        now = time.time()
        min_rows = mod._ROUTE_MIN_COMPLETE_ROWS
        healthy = [
            self._make_entry(t_start=now - 100, route="warm_server")
            for _ in range(min_rows + 5)
        ]
        timestampless_routeless = [self._make_entry(t_start=None, route=None)]
        monkeypatch.setattr(
            engine_report,
            "iter_sink_entries",
            lambda **kw: iter(healthy + timestampless_routeless),
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert result.status == mod._PASS
        # The untimestamped row is counted (survives every window) but does
        # not sink coverage below the floor.
        assert result.data["route_distribution"]["complete"] == min_rows + 6
        assert result.data["route_distribution"]["unstamped"] == 1

    def test_all_time_is_populated_and_differs_from_windowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC4 — `data["all_time"]` is populated and differs from the
        windowed figure when old, out-of-window rows exist alongside a
        healthy recent window."""
        mod = _require_module()

        from coordinator_core.telemetry import engine_report

        now = time.time()
        min_rows = mod._ROUTE_MIN_COMPLETE_ROWS
        recent = [
            self._make_entry(t_start=now - 100, route="warm_server")
            for _ in range(min_rows + 5)
        ]
        ancient = [
            self._make_entry(t_start=now - 1_000_000, route="in_process")
            for _ in range(min_rows + 5)
        ]
        monkeypatch.setattr(
            engine_report,
            "iter_sink_entries",
            lambda **kw: iter(recent + ancient),
        )

        result = mod._run_probe_warm_route_share(tmp_path)

        assert result.data["all_time"] is not None
        assert result.data["all_time"]["complete"] == len(recent) + len(ancient)
        assert result.data["all_time"]["complete"] != result.data["route_distribution"]["complete"]

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
