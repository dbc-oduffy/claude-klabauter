"""
bin.tests.test_claude_klabauter_doctor_new_probes — Unit tests for the C1b probes added by the
DR-215 doctor rebuild.

Covers four probes introduced in C1b, exercising healthy + fault paths via direct
function calls (no subprocess) — loads bin/claude-klabauter-doctor-probe.py as a module
via importlib for fast, isolated, monkeypatched execution.

Probes under test:
  claude-klabauter.resident.debris — detects stale daemon paths; INFO on found, PASS on absent, NEVER DEGRADED
  claude-klabauter.worktree.bloat  — detects large untracked/tracked files via filesystem walk; INFO on
                            found, PASS on absent, NEVER DEGRADED
  claude-klabauter.version.sanity  — coordinator_core importable; retired submodules absent
  claude-klabauter.invoke.smoke    — spawn-per-call dispatch smoke; SKIP on spawn failure, never crash
  claude-klabauter.execnet.orphaned_gateways — flags execnet gateways with no live controller;
                            AC4: a gateway under a LIVE controller must be PASS, never
                            flagged (docs/plans/2026-08-13-reap-orphaned-execnet-gateways.md § C2)

Probe-authoring invariant (per state/lessons/2026-07-04-a-diagnostic-must-always-emit-a-parseabl.yaml):
  Every probe must emit a parseable _ProbeResult on ALL paths — including its own
  bootstrap failure.  A bare exception or empty result is the exact failure the doctor
  exists to prevent.  Tests assert this invariant explicitly on fault paths.

Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C6
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
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_new_probes_unit"
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


class TestResidentDebrisProbe:
    """_run_probe_resident_debris() — debris present/absent paths.

    Key invariant: status must NEVER be DEGRADED (debris is harmless-but-stale
    per DR-215 negative-spec; INFO class matches version-drift advisory treatment).
    """

    def test_debris_absent_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        # Provide a CLAUDE_KLABAUTER_ROOT with no .git/coordinator-service/ sentinel.
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        if hasattr(mod.os, "getuid"):
            monkeypatch.setattr(mod.os, "getuid", lambda: 9_999_999)

        result = mod._run_probe_resident_debris(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.resident.debris"
        assert result.status == mod._PASS, (
            f"Expected PASS when no debris found, got {result.status!r}"
        )
        assert result.status != mod._DEGRADED, "debris probe must NEVER emit DEGRADED"

    def test_debris_git_sentinel_is_info(self, tmp_path: Path) -> None:
        """INFO when .git/coordinator-service/ sentinel exists.

        INFO is the correct signal — debris is harmless-but-stale post-DR-215.
        The probe must NEVER emit DEGRADED.
        """
        mod = _require_module()

        sentinel = tmp_path / ".git" / "coordinator-service"
        sentinel.mkdir(parents=True)

        result = mod._run_probe_resident_debris(tmp_path)

        assert _is_parseable_probe_result(result), (
            "debris-found path must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.resident.debris"
        assert result.status == mod._INFO, (
            f"Expected INFO when debris found (harmless-but-stale), got {result.status!r}"
        )
        assert result.status != mod._DEGRADED, (
            "claude-klabauter.resident.debris must NEVER emit DEGRADED — "
            "debris is an INFO advisory per DR-215 negative-spec"
        )
        assert result.status != mod._BROKEN, (
            "claude-klabauter.resident.debris must not emit BROKEN for presence of stale debris"
        )
        assert "coordinator-service" in result.detail or str(sentinel) in result.detail, (
            f"Detail should mention the debris path, got: {result.detail!r}"
        )

    def test_debris_never_emits_degraded_on_any_path(self, tmp_path: Path) -> None:
        """Regression guard: DEGRADED is reserved for genuine hard failures.

        Debris is harmless-but-stale; DEGRADED would mislead callers into thinking
        the engine is in a genuinely degraded operational state.
        """
        mod = _require_module()

        for root in [tmp_path, None]:
            if root is not None:
                sentinel = root / ".git" / "coordinator-service"
                sentinel.mkdir(parents=True, exist_ok=True)

            result = mod._run_probe_resident_debris(root)

            assert _is_parseable_probe_result(result), (
                f"debris probe must always return parseable result (root={root!r})"
            )
            assert result.status != mod._DEGRADED, (
                f"claude-klabauter.resident.debris MUST NEVER emit DEGRADED "
                f"(got {result.status!r} for root={root!r}); "
                "debris is harmless-but-stale per DR-215 negative-spec"
            )

    def test_debris_probe_always_emits_parseable_result(self) -> None:
        mod = _require_module()

        result = mod._run_probe_resident_debris(None)

        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.resident.debris"
        assert result.status in {mod._PASS, mod._INFO, mod._BROKEN}, (
            f"Unexpected status {result.status!r}; must be PASS, INFO, or BROKEN (never DEGRADED)"
        )
        assert result.status != mod._DEGRADED


class TestWorktreeBloatProbe:
    """_run_probe_worktree_bloat() — large-file-found / clean / .git-pruned / None-root paths.

    Motivation: a 365 GB untracked junk file (`correct?*`, from a mis-quoted shell
    redirect) sat undetected in the repo root for days. This probe scans the
    FILESYSTEM worktree (not `git ls-files`) so untracked junk is caught.

    Key invariant: status must NEVER be DEGRADED (large-file-found is an INFO
    advisory per the resident.debris precedent, not a hard failure).
    """

    def test_bloat_found_is_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INFO when a file exceeds the (tiny, test-overridden) threshold.

        Sets CLAUDE_KLABAUTER_DOCTOR_LARGE_FILE_BYTES to a tiny value so an ordinary small
        file trips the threshold without needing to write gigabytes to disk.
        """
        mod = _require_module()

        monkeypatch.setenv("CLAUDE_KLABAUTER_DOCTOR_LARGE_FILE_BYTES", "10")

        big_file = tmp_path / "junk.txt"
        big_file.write_bytes(b"x" * 100)

        result = mod._run_probe_worktree_bloat(tmp_path)

        assert _is_parseable_probe_result(result), (
            "large-file-found path must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.worktree.bloat"
        assert result.status == mod._INFO, (
            f"Expected INFO when a large file is found, got {result.status!r}"
        )
        assert result.status != mod._DEGRADED, (
            "claude-klabauter.worktree.bloat must NEVER emit DEGRADED — large-file-found is an "
            "INFO advisory"
        )
        assert result.data is not None
        found_paths = {f["path"] for f in result.data["large_files"]}
        assert "junk.txt" in found_paths, (
            f"Expected 'junk.txt' in flagged large_files, got {found_paths!r}"
        )

    def test_bloat_clean_tree_is_pass(self, tmp_path: Path) -> None:
        mod = _require_module()

        (tmp_path / "small.txt").write_text("hello")

        result = mod._run_probe_worktree_bloat(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.worktree.bloat"
        assert result.status == mod._PASS, (
            f"Expected PASS on a clean tree, got {result.status!r}"
        )
        assert result.data is not None
        assert result.data["large_files"] == []

    def test_bloat_git_dir_pruned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        monkeypatch.setenv("CLAUDE_KLABAUTER_DOCTOR_LARGE_FILE_BYTES", "10")

        git_dir = tmp_path / ".git" / "objects"
        git_dir.mkdir(parents=True)
        (git_dir / "big-blob").write_bytes(b"x" * 100)

        result = mod._run_probe_worktree_bloat(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.worktree.bloat"
        assert result.status == mod._PASS, (
            f"Expected PASS — the only large file lives under .git/ and must be pruned, "
            f"got {result.status!r} with data={result.data!r}"
        )
        assert result.data is not None
        assert result.data["large_files"] == [], (
            ".git/ contents must never appear in large_files"
        )

    def test_bloat_none_root_does_not_crash(self) -> None:
        mod = _require_module()

        result = mod._run_probe_worktree_bloat(None)

        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.worktree.bloat"
        assert result.status == mod._PASS, (
            f"Expected PASS (scan skipped) when claude_klabauter_root is None, got {result.status!r}"
        )
        assert result.status != mod._DEGRADED


class TestVersionSanityProbe:

    def test_version_sanity_healthy_repo_is_pass(self) -> None:
        mod = _require_module()

        result = mod._run_probe_version_sanity(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "version sanity probe must always return a parseable _ProbeResult"
        )
        assert result.probe == "claude-klabauter.version.sanity"
        assert result.status == mod._PASS, (
            f"Expected PASS on a healthy repo, got {result.status!r}; "
            f"detail: {result.detail!r}"
        )

    def test_version_sanity_none_root_emits_parseable_result(self) -> None:
        """No crash; returns PASS or BROKEN when claude_klabauter_root is None.

        Probe-authoring invariant: emit a parseable _ProbeResult even when
        CLAUDE_KLABAUTER_ROOT is unresolved; do not propagate an unhandled exception.
        With None root the probe still attempts import from the current sys.path,
        which succeeds on the dev tree — hence PASS or BROKEN (not guaranteed BROKEN).
        """
        mod = _require_module()

        result = mod._run_probe_version_sanity(None)

        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.version.sanity"
        assert result.status in {mod._PASS, mod._BROKEN}, (
            f"Unexpected status {result.status!r} when claude_klabauter_root=None; expected PASS or BROKEN"
        )

    def test_version_sanity_result_parseable(self) -> None:
        mod = _require_module()

        result = mod._run_probe_version_sanity(_REPO_ROOT)

        for attr in ("probe", "status", "detail", "remediation", "required"):
            assert hasattr(result, attr), (
                f"_ProbeResult missing required attribute {attr!r}"
            )
        assert result.probe == "claude-klabauter.version.sanity"
        assert isinstance(result.status, str) and len(result.status) > 0


class TestInvokeSmokeProbe:

    @pytest.fixture
    def stamped_root(self, tmp_path: Path) -> Path:
        """A root the probe will actually dispatch from.

        The dispatch arms below run only on a STAMPED engine root (DR-331) --
        `_REPO_ROOT` is a source clone and carries no stamp, so pointing them
        there would exercise the unstamped SKIP instead of the arm each one
        names. Patching the predicate is not an option: `_require_module()`
        re-execs the probe file per call, so a monkeypatch lands on a
        different module instance than the test's own.
        """
        stamp = tmp_path / "coordinator_core" / "_engine_stamp"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("sha-published")
        return tmp_path

    def test_live_source_clone_never_reports_broken(self) -> None:
        """The live claude-klabauter checkout is a SOURCE clone, and a healthy one must
        not read red.

        `_REPO_ROOT` carries no engine build stamp, so `ipc.py`'s stamp gate
        and `invoke.__main__`'s no-cold-fallback arm refuse dispatch from it
        by ruling (DR-331, DR-315 § 2). The probe dispatches from the
        published mirror instead (PASS/BROKEN on the mirror's real health),
        or reports inconclusive when no mirror is registered on this box.
        Grading the clone's own refusal is the one outcome ruled out, and it
        is what this probe did on every install from a working tree.
        """
        mod = _require_module()

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "invoke smoke probe must always return a parseable _ProbeResult"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        if result.data.get("dispatch_root") is None:
            assert result.status == mod._INFO, (
                f"no dispatch root is inconclusive, got {result.status!r}"
            )
            assert result.skipped is True
        else:
            assert Path(result.data["dispatch_root"]) != _REPO_ROOT, (
                "an unstamped clone must never be its own dispatch root"
            )
        assert result.required is False, (
            "claude-klabauter.invoke.smoke must always carry required=False (OPTIONAL probe)"
        )

    def test_invoke_smoke_spawn_failure_emits_skip_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path
    ) -> None:
        mod = _require_module()

        def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError("no such interpreter")

        monkeypatch.setattr(mod.subprocess, "run", _raise_fnf)

        result = mod._run_probe_invoke_smoke(stamped_root)

        assert _is_parseable_probe_result(result), (
            "spawn FileNotFoundError must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.skipped is True, (
            f"Expected skipped=True on spawn failure, got skipped={result.skipped!r}"
        )
        assert result.required is False, (
            "required must be False on all invoke.smoke paths (OPTIONAL probe)"
        )
        assert result.status == mod._INFO, (
            f"Expected INFO for a skipped optional probe (SKIP envelope), got {result.status!r}"
        )

    def test_invoke_smoke_timeout_emits_broken_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path
    ) -> None:
        mod = _require_module()
        import subprocess as _subprocess

        def _raise_timeout(*args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd=args[0] if args else "cmd", timeout=30)

        monkeypatch.setattr(mod.subprocess, "run", _raise_timeout)

        result = mod._run_probe_invoke_smoke(stamped_root)

        assert _is_parseable_probe_result(result), (
            "TimeoutExpired must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._BROKEN, (
            f"Expected BROKEN on TimeoutExpired, got {result.status!r}"
        )
        assert result.required is False

    def test_invoke_smoke_none_root_emits_skip(self) -> None:
        mod = _require_module()

        result = mod._run_probe_invoke_smoke(None)

        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.skipped is True, (
            f"Expected skipped=True when claude_klabauter_root is None, got {result.skipped!r}"
        )
        assert result.required is False

    def test_invoke_smoke_nonzero_exit_is_broken_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path
    ) -> None:
        mod = _require_module()

        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "invoke crashed"

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        result = mod._run_probe_invoke_smoke(stamped_root)

        assert _is_parseable_probe_result(result), (
            "non-zero exit must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._BROKEN, (
            f"Expected BROKEN on non-zero invoke exit, got {result.status!r}"
        )
        assert result.required is False

    def test_invoke_smoke_exit_zero_malformed_stdout_is_broken(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path
    ) -> None:
        mod = _require_module()

        class _FakeResult:
            returncode = 0
            stdout = "not json {{{"
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        result = mod._run_probe_invoke_smoke(stamped_root)

        assert _is_parseable_probe_result(result), (
            "exit-0 with malformed stdout must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._BROKEN, (
            f"Expected BROKEN when stdout is not valid JSON (even with exit 0), "
            f"got {result.status!r}"
        )
        assert result.required is False

    def test_invoke_smoke_pass_verifies_ok_field(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path
    ) -> None:
        """PASS when invoke returns a well-formed result envelope with result.ok=true.

        The invoke entrypoint emits a JSON-RPC envelope; the ping payload
        ({ok: true, ts: ...}) is nested under 'result'.
        """
        import json as _json
        mod = _require_module()

        ping_payload = _json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"ok": True, "ts": "2026-07-06T00:00:00Z"}
        })

        class _FakeResult:
            returncode = 0
            stdout = ping_payload
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        result = mod._run_probe_invoke_smoke(stamped_root)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._PASS, (
            f"Expected PASS when invoke returns ok=true, got {result.status!r}"
        )
        assert result.required is False
        assert "CAN dispatch" in result.detail or "can dispatch" in result.detail.lower(), (
            f"Probe detail must state the entrypoint CAN dispatch (scope discipline); "
            f"got: {result.detail!r}"
        )


class _FakeProc:

    def __init__(self, info: dict) -> None:
        self.info = info


def _make_fake_psutil(procs, alive_pids):
    import types

    fake = types.ModuleType("psutil")
    fake.process_iter = lambda attrs=None: iter(_FakeProc(p) for p in procs)
    fake.pid_exists = lambda pid: pid in alive_pids
    return fake


_GATEWAY_CMDLINE = [
    "python3",
    "-c",
    "import sys;exec(eval(sys.stdin.readline()))",
]


class TestOrphanedExecnetGatewaysProbe:

    def test_orphaned_gateway_no_live_controller_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEGRADED when a gateway's parent pid no longer exists."""
        mod = _require_module()

        procs = [{"pid": 4242, "ppid": 9999, "cmdline": _GATEWAY_CMDLINE}]
        fake_psutil = _make_fake_psutil(procs, alive_pids=set())
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert _is_parseable_probe_result(result), (
            "orphaned-gateway path must produce a parseable _ProbeResult"
        )
        assert result.probe == "claude-klabauter.execnet.orphaned_gateways"
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED when a gateway's controller is confirmed dead, "
            f"got {result.status!r}"
        )
        assert result.data is not None
        assert result.data["orphaned_pids"] == [4242]
        assert result.data["live_controlled_count"] == 0

    def test_gateway_reparented_to_init_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEGRADED when a gateway's ppid is 1 (reparented to init) — even though
        pid_exists(1) would trivially be True on a real box, the probe must not
        use that as evidence of a live controller."""
        mod = _require_module()

        procs = [{"pid": 5151, "ppid": 1, "cmdline": _GATEWAY_CMDLINE}]
        fake_psutil = _make_fake_psutil(procs, alive_pids={1})
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED, (
            f"Expected DEGRADED for a gateway reparented to init (ppid=1), "
            f"got {result.status!r}"
        )
        assert result.data["orphaned_pids"] == [5151]

    def test_gateway_under_live_controller_is_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        procs = [{"pid": 6161, "ppid": 7171, "cmdline": _GATEWAY_CMDLINE}]
        fake_psutil = _make_fake_psutil(procs, alive_pids={7171})
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.execnet.orphaned_gateways"
        assert result.status == mod._PASS, (
            f"AC4: a gateway under a live controller must be PASS, not flagged as "
            f"orphaned. Got {result.status!r} with data={result.data!r}"
        )
        assert result.data["orphaned_pids"] == []
        assert result.data["live_controlled_count"] == 1

    def test_zero_gateways_is_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _require_module()

        procs = [{"pid": 1, "ppid": 0, "cmdline": ["some-other-process"]}]
        fake_psutil = _make_fake_psutil(procs, alive_pids=set())
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.execnet.orphaned_gateways"
        assert result.status == mod._PASS, (
            f"Expected PASS when zero gateway processes are found, got {result.status!r}"
        )
        assert result.data["orphaned_pids"] == []
        assert result.data["live_controlled_count"] == 0

    def test_mixed_orphaned_and_live_reports_only_orphaned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        procs = [
            {"pid": 100, "ppid": 200, "cmdline": _GATEWAY_CMDLINE},
            {"pid": 101, "ppid": 999, "cmdline": _GATEWAY_CMDLINE},
        ]
        fake_psutil = _make_fake_psutil(procs, alive_pids={200})
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert result.status == mod._DEGRADED
        assert result.data["orphaned_pids"] == [101]
        assert result.data["live_controlled_count"] == 1

    def test_psutil_absent_emits_skip_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        monkeypatch.setitem(sys.modules, "psutil", None)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert _is_parseable_probe_result(result), (
            "psutil-absent path must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.execnet.orphaned_gateways"
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is False

    def test_parent_liveness_read_error_fails_closed_to_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()

        procs = [{"pid": 303, "ppid": 404, "cmdline": _GATEWAY_CMDLINE}]
        fake_psutil = _make_fake_psutil(procs, alive_pids=set())

        def _raise(pid):
            raise OSError("indeterminate liveness read")

        fake_psutil.pid_exists = _raise
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        result = mod._run_probe_orphaned_execnet_gateways()

        assert _is_parseable_probe_result(result)
        assert result.status == mod._PASS, (
            f"An indeterminate parent-liveness read must fail closed toward "
            f"'assume alive' (not orphaned), got {result.status!r}"
        )
        assert result.data["orphaned_pids"] == []


class TestInvokeSmokeDispatchRoot:

    @staticmethod
    def _stamp(root: Path) -> Path:
        stamp = root / "coordinator_core" / "_engine_stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("sha-published")
        return root

    def test_unstamped_clone_dispatches_from_the_published_mirror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mod = _require_module()

        clone = tmp_path / "source-clone"
        clone.mkdir()
        mirror = self._stamp(tmp_path / "mirror")

        from coordinator_core import engine_root as engine_root_mod

        monkeypatch.setattr(
            engine_root_mod, "published_engine_mirror_path", lambda: str(mirror)
        )

        cwds: list[object] = []

        class _FakeResult:
            returncode = 0
            stdout = '{"result": {"ok": true, "ts": 1}}'
            stderr = ""

        def _capture(*a, **kw):
            cwds.append(kw.get("cwd"))
            return _FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", _capture)

        result = mod._run_probe_invoke_smoke(clone)

        assert result.status == mod._PASS
        assert cwds == [str(mirror)], (
            "an unstamped clone must dispatch from the published mirror, not itself"
        )
        assert result.data["dispatch_root"] == str(mirror)

    def test_no_stamped_root_anywhere_is_inconclusive_not_broken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mod = _require_module()

        from coordinator_core import engine_root as engine_root_mod

        monkeypatch.setattr(
            engine_root_mod, "published_engine_mirror_path", lambda: None
        )

        spawned: list[object] = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: spawned.append(a))

        result = mod._run_probe_invoke_smoke(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is False
        assert result.data["dispatch_root"] is None
        assert spawned == [], "nothing to dispatch from means nothing is spawned"

    def test_stamped_root_dispatches_from_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mod = _require_module()

        root = self._stamp(tmp_path)
        cwds: list[object] = []

        class _FakeResult:
            returncode = 0
            stdout = '{"result": {"ok": true, "ts": 1}}'
            stderr = ""

        def _capture(*a, **kw):
            cwds.append(kw.get("cwd"))
            return _FakeResult()

        monkeypatch.setattr(mod.subprocess, "run", _capture)

        result = mod._run_probe_invoke_smoke(root)

        assert result.status == mod._PASS
        assert cwds == [str(root)]
