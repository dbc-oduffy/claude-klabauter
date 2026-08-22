"""
bin.tests.test_claude_klabauter_doctor_generation_probe — Unit tests for
Claude-klabauter.warm.generation (C5b).

Covers `_run_probe_warm_generation` — for EVERY resident warm server
process (discovered via the SHARED `_enumerate_resident_warm_servers`
enumeration, same site `_run_probe_warm_residency` uses), compares that
server's OWN breadcrumb pipe-name token segment against a freshly
recomputed `skew.compute_client_token(engine_root)` computed against that
SAME server's own engine root — never against `claude_klabauter_root` (this repo's
own root), which was the bug this probe was fixed to stop making.
PURE LOCAL READ: never connects (the C5a connect-and-close primitive,
`_warm_check_pipe_reachable`, is never called) and never elects
(`election.elect` is never called).

Loads bin/claude-klabauter-doctor-probe.py as a module via importlib, matching the
existing loader pattern in test_claude_klabauter_doctor_warm_probes.py — own module
key, so this file's module instance never collides with a sibling test
file's in sys.modules. Mocks `psutil.process_iter` the same way
test_claude_klabauter_doctor_warm_probes.py does (`_FakeProc` / `_make_fake_psutil`)
so no test spawns a real process or opens a real named pipe.

Covered:
  AC8 — a stale token is reported (DEGRADED) without a connect attempt:
        `_warm_check_pipe_reachable` and `election.elect` are both asserted
        never called.
  Bug fix — a resident server whose engine root DIFFERS from `claude_klabauter_root`
        has ITS OWN breadcrumb read (the original defect: the probe used to
        read `claude_klabauter_root`'s breadcrumb regardless of which engine root the
        resident server actually ran from).
  Multiple resident servers — ANY stale server's token makes the overall
        verdict DEGRADED (naming every stale pid); absent any stale token,
        ANY cannot-tell server keeps the verdict from being a false PASS.
  Re-run of test_selector_default_returns_every_manifest_probe (AC6) lives
  in coordinator_core/tests/test_claude_klabauter_doctor_probe_selectors.py, not
  here — the manifest addition self-registers via that test's own
  `_IMPLEMENTED_IDS` derivation.

Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C5b.
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
    _KEY = "claude_klabauter_doctor_probe_generation_probe_unit"
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
    """Stand-in for a psutil.Process yielded by process_iter(attrs).

    Mirrors test_claude_klabauter_doctor_warm_probes.py's fixture exactly — kept
    local (rather than imported) so this file's fixtures stay self-
    contained, matching that file's own comment on why the loader pattern
    is duplicated rather than shared.
    """

    def __init__(self, info: dict) -> None:
        self.info = info


def _make_fake_psutil(procs):
    """Minimal fake psutil exposing only process_iter."""
    import types

    fake = types.ModuleType("psutil")
    fake.process_iter = lambda attrs=None: iter(_FakeProc(p) for p in procs)
    return fake


def _server_cmdline(engine_root: Path) -> list[str]:
    script = engine_root / "coordinator_core" / "warm" / "server.py"
    return ["python.exe", str(script)]


def _write_breadcrumb_for_root(
    engine_root: Path, monkeypatch: pytest.MonkeyPatch, pipe: str, pid: int
) -> None:
    """Write a real breadcrumb for `engine_root`, isolated under a
    tmp_path-scoped LOCALAPPDATA so `breadcrumb.read_breadcrumb(engine_root)`
    finds it without touching the real machine-wide breadcrumb directory."""
    from coordinator_core.warm import breadcrumb

    breadcrumb.write_breadcrumb(
        pipe=pipe,
        pid=pid,
        stable_pid_start_epoch=0,
        engine_sha=None,
        engine_root=engine_root,
    )


class TestWarmGenerationProbe:
    """_run_probe_warm_generation() — AC8."""

    def test_claude_klabauter_root_none_is_info_skipped(self) -> None:
        mod = _require_module()

        result = mod._run_probe_warm_generation(None)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is True

    def test_no_resident_server_is_cannot_tell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No resident warm server at all is a legitimate 'cannot tell'."""
        mod = _require_module()

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        fake_psutil = _make_fake_psutil([])
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is True
        assert result.data["servers"] == []

    def test_breadcrumb_absent_for_resident_server_is_cannot_tell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        engine_root = tmp_path  # no breadcrumb written for this root
        procs = [{"pid": 900, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is True

    def test_matching_token_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        from coordinator_core.warm import skew

        engine_root = tmp_path
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        skew.write_engine_stamp(engine_root, "sha-matching-token")
        current_token = skew.compute_client_token(engine_root)
        _write_breadcrumb_for_root(
            engine_root, monkeypatch,
            pipe=f"\\\\.\\pipe\\coordinator-core.sid.hash.{current_token}",
            pid=901,
        )
        procs = [{"pid": 901, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._PASS
        assert result.required is True
        assert result.data["servers"][0]["breadcrumb_pipe_token"] == current_token

    def test_stale_token_is_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC8 — a stale token is reported DEGRADED without a connect attempt."""
        mod = _require_module()

        from coordinator_core.warm import skew

        engine_root = tmp_path
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        skew.write_engine_stamp(engine_root, "sha-stale-token")
        _write_breadcrumb_for_root(
            engine_root, monkeypatch,
            pipe="\\\\.\\pipe\\coordinator-core.sid.hash.deadbeefdeadbeef",
            pid=902,
        )
        procs = [{"pid": 902, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        connect_called = []
        elect_called = []
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: connect_called.append(pipe))

        from coordinator_core.warm import election

        monkeypatch.setattr(election, "elect", lambda *a, **k: elect_called.append((a, k)))

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED, (
            f"AC8: a stale breadcrumb token must be reported DEGRADED, got {result.status!r}"
        )
        assert result.required is False, (
            "the stale arm names no action and must not gate setup.py's exit 94"
        )
        assert result.data["stale_pids"] == [902]
        assert connect_called == [], "AC8: the C5a connect helper must never be called by this probe"
        assert elect_called == [], "election.elect() must never be called by a read-only probe"

    def test_malformed_breadcrumb_pipe_is_cannot_tell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        engine_root = tmp_path
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        _write_breadcrumb_for_root(engine_root, monkeypatch, pipe="no-dot-separator", pid=903)
        procs = [{"pid": 903, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is True

    def test_token_computation_failure_is_cannot_tell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        engine_root = tmp_path
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        _write_breadcrumb_for_root(
            engine_root, monkeypatch,
            pipe="\\\\.\\pipe\\coordinator-core.sid.hash.sometoken",
            pid=904,
        )
        procs = [{"pid": 904, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        from coordinator_core.warm import skew

        monkeypatch.setattr(skew, "compute_client_token", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is True

    def test_resident_server_engine_root_differs_from_claude_klabauter_root_uses_own_breadcrumb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bug fix: a resident server whose engine root differs from
        `claude_klabauter_root` must have ITS OWN breadcrumb read — the original
        defect resolved `claude_klabauter_root`'s breadcrumb regardless of which
        engine root the resident server actually ran from, so a server
        running out of a published mirror (no breadcrumb under
        `claude_klabauter_root`) always read as 'no breadcrumb — cannot tell', even
        though its own clone had a real breadcrumb on disk."""
        mod = _require_module()

        from coordinator_core.warm import skew

        claude_klabauter_root = tmp_path / "this-repo"
        other_engine_root = tmp_path / "published-mirror"
        claude_klabauter_root.mkdir()
        other_engine_root.mkdir()

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        # Deliberately NO breadcrumb written for claude_klabauter_root.
        skew.write_engine_stamp(other_engine_root, "sha-mirror-token")
        current_token = skew.compute_client_token(other_engine_root)
        _write_breadcrumb_for_root(
            other_engine_root, monkeypatch,
            pipe=f"\\\\.\\pipe\\coordinator-core.sid.hash.{current_token}",
            pid=905,
        )
        procs = [{"pid": 905, "create_time": 0.0, "cmdline": _server_cmdline(other_engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        result = mod._run_probe_warm_generation(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert "no breadcrumb" not in result.detail.lower(), (
            "must not report 'no breadcrumb' when the resident server's OWN engine "
            f"root has a real one on disk: {result.detail!r}"
        )
        assert result.status == mod._PASS
        assert result.data["servers"][0]["engine_root"] == str(other_engine_root.resolve())
        assert result.data["servers"][0]["breadcrumb_pipe_token"] == current_token

    def test_multiple_resident_servers_any_stale_makes_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple resident servers is a real case: one current + one stale
        must produce DEGRADED naming the stale pid, not a PASS that hides it."""
        mod = _require_module()

        from coordinator_core.warm import skew

        current_root = tmp_path / "current-engine"
        stale_root = tmp_path / "stale-engine"
        current_root.mkdir()
        stale_root.mkdir()

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

        skew.write_engine_stamp(current_root, "sha-current")
        current_token = skew.compute_client_token(current_root)
        _write_breadcrumb_for_root(
            current_root, monkeypatch,
            pipe=f"\\\\.\\pipe\\coordinator-core.sid.hash.{current_token}",
            pid=906,
        )

        skew.write_engine_stamp(stale_root, "sha-stale")
        _write_breadcrumb_for_root(
            stale_root, monkeypatch,
            pipe="\\\\.\\pipe\\coordinator-core.sid.hash.deadbeefdeadbeef",
            pid=907,
        )

        procs = [
            {"pid": 906, "create_time": 0.0, "cmdline": _server_cmdline(current_root)},
            {"pid": 907, "create_time": 0.0, "cmdline": _server_cmdline(stale_root)},
        ]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        result = mod._run_probe_warm_generation(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.data["stale_pids"] == [907]
        assert len(result.data["servers"]) == 2

    def test_stale_generation_does_not_gate_step_zero_exit(self) -> None:
        """A remediation naming no action cannot be an install gate.

        `_sz_severity` maps `required` to step-zero `hard`, and
        `scripts/setup.py` returns EXIT_HEALTH_PROBE_HARD_FAILURE (94) for
        any hard probe whose status is not `pass`. The stale-generation arm
        drains on its own, so it must emit `advisory` and leave the exit
        code at 0 — otherwise every install on a box with an in-flight warm
        server reports failure.
        """
        mod = _require_module()

        stale = mod._ProbeResult(
            probe=mod._WARM_GENERATION_PROBE,
            status=mod._DEGRADED,
            detail="1 resident warm server process(es) have a stale generation token.",
            remediation=(
                "A stale generation drains on its own via warm.idle's superseded-"
                "generation arm once a fresh server binds; no direct action is named here."
            ),
            required=False,
        )

        assert mod._sz_severity(stale) == mod._SZ_ADVISORY
        assert mod.emit_step_zero([stale]) == 0

    def test_psutil_never_reaches_election_elect_module_wide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No connect and no election, even on the PASS path — not just the
        DEGRADED path AC8 pins."""
        mod = _require_module()

        from coordinator_core.warm import election, skew

        engine_root = tmp_path
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
        skew.write_engine_stamp(engine_root, "sha-pass-path")
        current_token = skew.compute_client_token(engine_root)
        _write_breadcrumb_for_root(
            engine_root, monkeypatch,
            pipe=f"\\\\.\\pipe\\coordinator-core.sid.hash.{current_token}",
            pid=908,
        )
        procs = [{"pid": 908, "create_time": 0.0, "cmdline": _server_cmdline(engine_root)}]
        monkeypatch.setitem(sys.modules, "psutil", _make_fake_psutil(procs))

        connect_called = []
        elect_called = []
        monkeypatch.setattr(mod, "_warm_check_pipe_reachable", lambda pipe: connect_called.append(pipe))
        monkeypatch.setattr(election, "elect", lambda *a, **k: elect_called.append((a, k)))

        result = mod._run_probe_warm_generation(tmp_path)

        assert result.status == mod._PASS
        assert connect_called == []
        assert elect_called == []
