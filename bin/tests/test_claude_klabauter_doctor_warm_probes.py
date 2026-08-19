"""
bin.tests.test_claude_klabauter_doctor_warm_probes — Unit tests for claude-klabauter.warm.residency (C5a).

Covers `_run_probe_warm_residency` — resident warm server enumeration via
psutil.process_iter cmdline matching, plus its REACHABLE/ORPHAN classification
using the hand-rolled connect-and-close pipe primitive
(`_warm_check_pipe_reachable`).

Loads bin/claude-klabauter-doctor-probe.py as a module via importlib, matching the
existing `_make_fake_psutil` pattern in test_claude_klabauter_doctor_new_probes.py — no
test spawns a real server or opens a real named pipe; `_warm_check_pipe_reachable`
itself is monkeypatched per-scenario so classification is deterministic and
platform-independent.

Covered:
  AC7  — a resident server whose pipe is unaddressable is reported ORPHAN, not
         silently PASS.
  AC9  — breadcrumb absence yields per-server "cannot_tell", never a claim of
         "no server running".
  AC10 — the orphan-path remediation names no action ("warm-engine-stop" does
         not appear).
  Re-run of test_selector_default_returns_every_manifest_probe (AC6) lives in
  coordinator_core/tests/test_claude_klabauter_doctor_probe_selectors.py, not here — the
  manifest addition self-registers via that test's own `_IMPLEMENTED_IDS`
  derivation.

Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C5a.
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

    Mirrors test_claude_klabauter_doctor_new_probes.py's loader exactly (own module
    key, so the two test files' module instances never collide in
    sys.modules).
    """
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_warm_probes_unit"
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
        import coordinator_core.warm.breadcrumb  # noqa: F401
        import coordinator_core.warm.election  # noqa: F401
        import coordinator_core.warm.skew  # noqa: F401
        import coordinator_core.session.core  # noqa: F401
    except ImportError:
        pytest.skip("coordinator_core.warm not importable in this environment")
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


class _FakeProc:
    """Stand-in for a psutil.Process yielded by process_iter(attrs)."""

    def __init__(self, info: dict) -> None:
        self.info = info


def _make_fake_psutil(procs):
    """Minimal fake psutil exposing only process_iter — the sole primitive
    `_run_probe_warm_residency` calls on the module directly (reachability
    itself is exercised via `_warm_check_pipe_reachable`, monkeypatched
    separately per test)."""
    import types

    fake = types.ModuleType("psutil")
    fake.process_iter = lambda attrs=None: iter(_FakeProc(p) for p in procs)
    return fake


def _server_cmdline(engine_root: Path) -> list[str]:
    script = engine_root / "coordinator_core" / "warm" / "server.py"
    return ["python.exe", str(script)]


def _stub_pipe_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the token/pipe-name resolution the residency probe performs
    BEFORE it reaches `_warm_check_pipe_reachable`.

    Stubbing only the reachability primitive is not enough to exercise
    classification: the probe first calls `skew.compute_client_token` and
    `election.pipe_name` against the server's own engine root, which for a
    synthetic `tmp_path` root raises, and the probe's outer guard correctly
    turns any such failure into `classification="cannot_tell"`. The
    reachability stub is then never consulted and every classification
    assertion reads `cannot_tell`. Verified against the live box, where the
    unstubbed path resolves and classifies `reachable` — so this is the
    fixture missing a seam, not the probe declining to classify.
    """
    from coordinator_core.warm import election, skew

    monkeypatch.setattr(skew, "compute_client_token", lambda engine_root: "stub-token")
    monkeypatch.setattr(
        election, "pipe_name", lambda token, engine_clone=None: r"\\.\pipe\stub-" + str(token)
    )


class TestWarmResidencyProbe:
    """_run_probe_warm_residency() — AC7, AC9, AC10."""

    def test_no_resident_servers_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        fake_psutil = _make_fake_psutil([])
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_warm_residency(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.warm.residency"
        assert result.status == mod._PASS
        assert result.data["servers"] == []

    def test_reachable_server_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC7's healthy counterpart: a resident, addressable server is PASS."""
        mod = _require_module()

        engine_root = tmp_path / "engine"
        procs = [{"pid": 111, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        fake_psutil = _make_fake_psutil(procs)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        _stub_pipe_resolution(monkeypatch)
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: (True, False))

        result = mod._run_probe_warm_residency(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._PASS
        assert result.required is True
        assert len(result.data["servers"]) == 1
        assert result.data["servers"][0]["classification"] == "reachable"
        assert result.data["servers"][0]["pid"] == 111

    def test_resident_unaddressable_server_is_orphan_not_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC7 — the load-bearing case: a resident-but-unreachable server must
        be reported ORPHAN via DEGRADED, never silently PASS."""
        mod = _require_module()

        engine_root = tmp_path / "engine"
        procs = [{"pid": 222, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        fake_psutil = _make_fake_psutil(procs)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        _stub_pipe_resolution(monkeypatch)
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: (False, False))

        result = mod._run_probe_warm_residency(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED for a resident-but-unreachable server, got {result.status!r}"
        )
        assert result.required is True
        assert result.data["orphan_pids"] == [222]
        assert result.data["servers"][0]["classification"] == "orphan"

    def test_orphan_remediation_names_no_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC10 — the orphan condition names NO remediation. `warm-engine-stop`
        targets the current, breadcrumb-elected server; naming it here would
        risk killing the live server while leaving the orphan running."""
        mod = _require_module()

        engine_root = tmp_path / "engine"
        procs = [{"pid": 333, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        fake_psutil = _make_fake_psutil(procs)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        _stub_pipe_resolution(monkeypatch)
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: (False, False))

        result = mod._run_probe_warm_residency(tmp_path)

        assert "warm-engine-stop" not in result.remediation, (
            f"Orphan remediation must not name the stop mechanism directly: {result.remediation!r}"
        )
        assert result.remediation != "—"

    def test_breadcrumb_absent_yields_cannot_tell_not_no_server_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC9 — breadcrumb absence enriches per-server state as "cannot_tell",
        never a claim that no server is running (a resident server WAS found
        via psutil; the breadcrumb is orthogonal enrichment only)."""
        mod = _require_module()

        engine_root = tmp_path / "engine"
        procs = [{"pid": 444, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        fake_psutil = _make_fake_psutil(procs)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        _stub_pipe_resolution(monkeypatch)
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: (True, False))
        # No breadcrumb file exists under tmp_path/engine — read_breadcrumb returns None.

        result = mod._run_probe_warm_residency(tmp_path)

        assert _is_parseable_probe_result(result)
        server_row = result.data["servers"][0]
        assert server_row["breadcrumb_state"] == "cannot_tell"
        assert "no server running" not in result.detail.lower()
        assert "no server running" not in result.remediation.lower()

    def test_reachability_indeterminate_reports_cannot_tell_required_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the reachability primitive itself is skipped (e.g. POSIX),
        the probe reports skipped=True with required=True (AC9/AC10's intent
        — stated explicitly, not read off the TOML manifest footer)."""
        mod = _require_module()

        engine_root = tmp_path / "engine"
        procs = [{"pid": 555, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        fake_psutil = _make_fake_psutil(procs)
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        _stub_pipe_resolution(monkeypatch)
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: (None, True))

        result = mod._run_probe_warm_residency(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.skipped is True
        assert result.required is True
        assert result.data["servers"][0]["classification"] == "cannot_tell"

    def test_psutil_missing_is_info_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors _run_probe_orphaned_execnet_gateways's ImportError guard shape."""
        mod = _require_module()

        real_import = __import__

        def _fail_psutil_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *args, **kwargs)

        monkeypatch.setitem(sys.modules, "psutil", None)
        monkeypatch.setattr("builtins.__import__", _fail_psutil_import)

        result = mod._run_probe_warm_residency(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True

    def test_claude_klabauter_root_none_is_info_skipped(self) -> None:
        mod = _require_module()

        result = mod._run_probe_warm_residency(None)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
