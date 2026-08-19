"""
bin.tests.test_claude_klabauter_doctor_warm_roundtrip_probe — Unit tests for
Claude-klabauter.warm.roundtrip (C9).

Covers `_run_probe_warm_roundtrip` — the opt-in live warm-server round-trip
probe. Loads bin/claude-klabauter-doctor-probe.py as a module via importlib, matching
the existing `_make_fake_psutil`/stubbed-transport pattern in
test_claude_klabauter_doctor_warm_probes.py — `coordinator_core.warm.client.try_warm_dispatch`
is monkeypatched per-scenario so no test opens a real named pipe or spawns a
real warm server.

Covered:
  - Flag-default-off path: no connection attempted, `skipped=True`.
  - Hang-timeout path (thread still alive at the join deadline).
  - Unexpected-error path (`result_box["error"]`).
  - The P1 property directly: neither DEGRADED-but-not-gating path drags an
    otherwise-all-PASS envelope's `overall` below PASS — asserted against
    `_build_envelope_via_module`'s emitted envelope, not `_local_reduce_overall`
    called directly (a normal run goes through
    `coordinator_core.doctor_envelope.build_envelope`, so asserting the local
    fallback alone would pass even if the live reducer regressed).

Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C9.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Own module key so this test file's module instance never collides in
    sys.modules with the sibling warm-probe test files.
    """
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_warm_roundtrip_unit"
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
        import coordinator_core.warm.client  # noqa: F401
    except ImportError:
        pytest.skip("coordinator_core.warm.client not importable in this environment")
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


class TestWarmRoundtripProbeDefaultOff:
    """`include_live_roundtrip=False` — the flag-default-off path."""

    def test_default_off_skips_without_attempting_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        from coordinator_core.warm import client as warm_client

        def _fail_if_called(msg):
            raise AssertionError("try_warm_dispatch must not be called when opted out")

        monkeypatch.setattr(warm_client, "try_warm_dispatch", _fail_if_called)

        result = mod._run_probe_warm_roundtrip(tmp_path, include_live_roundtrip=False)

        assert _is_parseable_probe_result(result)
        assert result.probe == mod._WARM_ROUNDTRIP_PROBE
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is False


class TestWarmRoundtripProbeHangTimeout:
    """Thread still alive at the join deadline — a hang, not a clean miss."""

    def test_hang_timeout_is_degraded_and_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        from coordinator_core.warm import client as warm_client

        monkeypatch.setattr(mod, "_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(
            warm_client, "try_warm_dispatch", lambda msg: time.sleep(2) or None
        )

        result = mod._run_probe_warm_roundtrip(tmp_path, include_live_roundtrip=True)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.required is False
        assert result.skipped is True, (
            "P1: the hang-timeout path must pair required=False with skipped=True "
            "or _local_reduce_overall gates on status regardless of required"
        )

    def test_hang_timeout_does_not_gate_envelope_overall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1, asserted at the envelope level via `_build_envelope_via_module`
        (never `_local_reduce_overall` called directly) — the same shape as
        route_share's own envelope-level regression test."""
        mod = _require_module()
        from coordinator_core.warm import client as warm_client

        monkeypatch.setattr(mod, "_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(
            warm_client, "try_warm_dispatch", lambda msg: time.sleep(2) or None
        )

        roundtrip_result = mod._run_probe_warm_roundtrip(tmp_path, include_live_roundtrip=True)
        assert roundtrip_result.status == mod._DEGRADED

        all_pass_probe = mod._ProbeResult(
            probe="claude-klabauter.core.import",
            status=mod._PASS,
            detail="ok",
            remediation="—",
            required=True,
        )

        envelope = mod._build_envelope_via_module(
            [all_pass_probe, roundtrip_result], tmp_path
        )

        assert envelope["overall"] == mod._PASS, (
            f"P1: a hung, required=False roundtrip probe must never gate an "
            f"otherwise-all-PASS envelope's overall, got {envelope['overall']!r}"
        )


class TestWarmRoundtripProbeUnexpectedError:
    """`result_box['error']` — try_warm_dispatch raised despite its own contract."""

    def test_unexpected_error_is_degraded_and_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        from coordinator_core.warm import client as warm_client

        def _boom(msg):
            raise RuntimeError("boom")

        monkeypatch.setattr(warm_client, "try_warm_dispatch", _boom)

        result = mod._run_probe_warm_roundtrip(tmp_path, include_live_roundtrip=True)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.required is False
        assert result.skipped is True, (
            "P1: the unexpected-error path must pair required=False with "
            "skipped=True or _local_reduce_overall gates on status regardless "
            "of required"
        )
        assert "boom" in result.detail

    def test_unexpected_error_does_not_gate_envelope_overall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1, asserted at the envelope level via `_build_envelope_via_module`."""
        mod = _require_module()
        from coordinator_core.warm import client as warm_client

        def _boom(msg):
            raise RuntimeError("boom")

        monkeypatch.setattr(warm_client, "try_warm_dispatch", _boom)

        roundtrip_result = mod._run_probe_warm_roundtrip(tmp_path, include_live_roundtrip=True)
        assert roundtrip_result.status == mod._DEGRADED

        all_pass_probe = mod._ProbeResult(
            probe="claude-klabauter.core.import",
            status=mod._PASS,
            detail="ok",
            remediation="—",
            required=True,
        )

        envelope = mod._build_envelope_via_module(
            [all_pass_probe, roundtrip_result], tmp_path
        )

        assert envelope["overall"] == mod._PASS, (
            f"P1: an unexpected-error, required=False roundtrip probe must "
            f"never gate an otherwise-all-PASS envelope's overall, got "
            f"{envelope['overall']!r}"
        )
