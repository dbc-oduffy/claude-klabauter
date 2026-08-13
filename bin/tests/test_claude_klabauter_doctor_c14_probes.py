"""
bin.tests.test_claude_klabauter_doctor_c14_probes — Unit tests for the C14 Windows-portability
probes added to bin/claude-klabauter-doctor-probe.py.

Covers two probes introduced in C14, exercising healthy + fault paths via direct
function calls (no subprocess) — loads bin/claude-klabauter-doctor-probe.py as a module
via importlib for fast, isolated, monkeypatched execution.

Probes under test:
  claude-klabauter.root.pointer   — claude-klabauter-root pointer file present at
                          <settings-home>/machine-local/.claude-klabauter-root and matches the
                          resolved CLAUDE_KLABAUTER_ROOT; DEGRADED (not hard FAIL) on absence/mismatch.
  claude-klabauter.invoke.latency — measures a single coordinator_core.invoke round-trip against a
                          2000 ms budget; DEGRADED (not BROKEN) over budget or on timeout.

Probe-authoring invariant (per state/lessons/2026-07-04-a-diagnostic-must-always-emit-a-parseabl.yaml):
  Every probe must emit a parseable _ProbeResult on ALL paths — including its own
  bootstrap failure.  A bare exception or empty result is the exact failure the doctor
  exists to prevent.  Tests assert this invariant explicitly on fault paths.

Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C14
"""

from __future__ import annotations

import importlib.util
import subprocess as _subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Returns None if loading fails (caller should pytest.skip).
    Each call produces a fresh module instance — safe to monkeypatch in isolation.

    The module is registered in sys.modules under a unique key before exec so
    that Python's dataclass annotation-resolution path (sys.modules[cls.__module__])
    finds a valid namespace on Python 3.14+.
    """
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_c14_probes_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module so dataclass __module__ lookups succeed.
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
    return mod  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# claude-klabauter.root.pointer tests
# ---------------------------------------------------------------------------


class TestRootPointerProbe:
    """_run_probe_root_pointer() — absent / matched / mismatched / None-root paths.

    Key invariant: absence or mismatch is DEGRADED (actionable, not hard FAIL) with
    required=False — never BROKEN except on the probe's own unexpected-exception path.
    """

    def test_pointer_absent_is_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEGRADED (not BROKEN) when the pointer file is absent.

        Points COORDINATOR_SETTINGS_HOME at an empty tmp_path so no real pointer
        on this machine leaks into the test.
        """
        mod = _require_module()

        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))

        claude_klabauter_root = tmp_path / "claude-klabauter-checkout"
        claude_klabauter_root.mkdir()

        result = mod._run_probe_root_pointer(claude_klabauter_root)

        assert _is_parseable_probe_result(result), (
            "pointer-absent path must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.root.pointer"
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED when pointer absent (actionable, not hard FAIL), "
            f"got {result.status!r}"
        )
        assert result.status != mod._BROKEN, (
            "claude-klabauter.root.pointer must not emit BROKEN for a merely-absent pointer"
        )
        assert result.required is False, (
            "claude-klabauter.root.pointer must carry required=False (WARN, not hard FAIL)"
        )
        assert isinstance(result.remediation, str) and len(result.remediation) > 0
        assert "gen-claude-klabauter-root-pointer" in result.remediation, (
            f"Remediation should point at the install-time writer, got: {result.remediation!r}"
        )

    def test_pointer_present_and_matches_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PASS when the pointer exists and its content matches the resolved root."""
        mod = _require_module()

        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))

        claude_klabauter_root = tmp_path / "claude-klabauter-checkout"
        claude_klabauter_root.mkdir()

        pointer_dir = tmp_path / "machine-local"
        pointer_dir.mkdir()
        (pointer_dir / ".claude-klabauter-root").write_text(str(claude_klabauter_root))

        result = mod._run_probe_root_pointer(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.root.pointer"
        assert result.status == mod._PASS, (
            f"Expected PASS when pointer present and matches resolved root, "
            f"got {result.status!r}; detail: {result.detail!r}"
        )
        assert result.required is False

    def test_pointer_present_but_mismatched_is_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEGRADED (stale pointer) when content diverges from the resolved root."""
        mod = _require_module()

        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))

        claude_klabauter_root = tmp_path / "claude-klabauter-checkout"
        claude_klabauter_root.mkdir()
        other_root = tmp_path / "some-other-checkout"
        other_root.mkdir()

        pointer_dir = tmp_path / "machine-local"
        pointer_dir.mkdir()
        (pointer_dir / ".claude-klabauter-root").write_text(str(other_root))

        result = mod._run_probe_root_pointer(claude_klabauter_root)

        assert _is_parseable_probe_result(result), (
            "mismatched-pointer path must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.root.pointer"
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED for a stale/mismatched pointer, got {result.status!r}"
        )
        assert result.required is False

    def test_pointer_none_root_still_checks_presence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude_klabauter_root=None still reports pointer presence; content-match is skipped."""
        mod = _require_module()

        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))

        pointer_dir = tmp_path / "machine-local"
        pointer_dir.mkdir()
        (pointer_dir / ".claude-klabauter-root").write_text("/some/path")

        result = mod._run_probe_root_pointer(None)

        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.root.pointer"
        assert result.status == mod._PASS, (
            f"Expected PASS (presence-only check) when claude_klabauter_root is None, "
            f"got {result.status!r}"
        )
        assert result.required is False


# ---------------------------------------------------------------------------
# claude-klabauter.invoke.latency tests
# ---------------------------------------------------------------------------


class TestInvokeLatencyProbe:
    """_run_probe_invoke_latency() — under-budget / over-budget / timeout / None-root paths.

    Key invariant: over-budget and timeout are DEGRADED (WARN), never BROKEN, and the
    subprocess call is timeout-guarded so this probe can never hang the doctor.
    """

    def test_latency_under_budget_is_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PASS when the (mocked) round-trip completes well under budget."""
        mod = _require_module()

        class _FakeResult:
            returncode = 0
            stdout = '{"result": {"ok": true}}'
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        result = mod._run_probe_invoke_latency(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.status == mod._PASS, (
            f"Expected PASS for a fast mocked round-trip, got {result.status!r}"
        )
        assert result.required is False
        assert result.data is not None
        assert result.data["timed_out"] is False
        assert result.data["budget_ms"] == mod._INVOKE_LATENCY_BUDGET_MS

    def test_latency_over_budget_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEGRADED (not BROKEN) when the measured elapsed time exceeds the budget.

        Monkeypatches time.perf_counter to fabricate an elapsed time comfortably
        over _INVOKE_LATENCY_BUDGET_MS without an actual slow subprocess.
        """
        mod = _require_module()

        class _FakeResult:
            returncode = 0
            stdout = '{"result": {"ok": true}}'
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        _times = iter([0.0, (mod._INVOKE_LATENCY_BUDGET_MS / 1000.0) + 1.0])
        monkeypatch.setattr(mod.time, "perf_counter", lambda: next(_times))

        result = mod._run_probe_invoke_latency(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "over-budget path must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED when over budget, got {result.status!r}"
        )
        assert result.status != mod._BROKEN, (
            "claude-klabauter.invoke.latency must not emit BROKEN for a merely-slow round-trip"
        )
        assert result.required is False
        assert "claude-klabauter-root pointer" in result.remediation

    def test_latency_timeout_is_degraded_not_broken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEGRADED (not BROKEN, not a hang) when the bounded subprocess call times out.

        A timeout IS the failure being detected — the probe must survive it and
        emit a DEGRADED verdict, never propagate the exception or hang itself.
        """
        mod = _require_module()

        def _raise_timeout(*args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd=args[0] if args else "cmd", timeout=5)

        monkeypatch.setattr(mod.subprocess, "run", _raise_timeout)

        result = mod._run_probe_invoke_latency(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "TimeoutExpired must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED on timeout (that IS the failure being detected), "
            f"got {result.status!r}"
        )
        assert result.required is False
        assert result.data is not None
        assert result.data["timed_out"] is True

    def test_latency_spawn_failure_emits_skip_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKIP (not a crash) when subprocess.run raises FileNotFoundError (interpreter absent)."""
        mod = _require_module()

        def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError("no such interpreter")

        monkeypatch.setattr(mod.subprocess, "run", _raise_fnf)

        result = mod._run_probe_invoke_latency(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "spawn FileNotFoundError must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.skipped is True
        assert result.required is False
        assert result.status == mod._INFO

    def test_latency_none_root_emits_skip(self) -> None:
        """SKIP (not a crash) when claude_klabauter_root is None."""
        mod = _require_module()

        result = mod._run_probe_invoke_latency(None)

        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.skipped is True
        assert result.required is False

    def test_latency_healthy_repo_emits_parseable_result(self) -> None:
        """Real (non-mocked) invocation on the healthy dev repo emits a parseable result.

        Status is PASS/DEGRADED/BROKEN depending on real machine timing/health — the
        test verifies the invariant (parseable, never a crash, never hangs the test
        run itself since the probe is timeout-guarded), not a specific verdict.
        """
        mod = _require_module()

        result = mod._run_probe_invoke_latency(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.required is False
        assert result.status in {mod._PASS, mod._DEGRADED, mod._BROKEN, mod._INFO}
