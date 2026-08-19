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
    _KEY = "claude_klabauter_doctor_probe_new_probes_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec_module so dataclass __module__ lookups succeed.
    # Review: code-reviewer — F10: added comment (same guard as selector test version).
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
# claude-klabauter.resident.debris tests
# ---------------------------------------------------------------------------


class TestResidentDebrisProbe:
    """_run_probe_resident_debris() — debris present/absent paths.

    Key invariant: status must NEVER be DEGRADED (debris is harmless-but-stale
    per DR-215 negative-spec; INFO class matches version-drift advisory treatment).
    """

    def test_debris_absent_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PASS when no stale debris paths exist.

        On POSIX, monkeypatches os.getuid() to a high sentinel uid so
        /tmp/coordinator-svc-<uid>/ is guaranteed absent on any machine,
        isolating the test from real on-disk state. On Windows os.getuid does
        not exist, the probe's own AttributeError guard skips the socket-dir
        check entirely, and that isolation is structural rather than patched.
        """
        mod = _require_module()

        # Provide a CLAUDE_KLABAUTER_ROOT with no .git/coordinator-service/ sentinel.
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        # Force uid to a value that cannot have a real socket dir. Guarded rather
        # than passed `raising=False`: injecting a synthetic getuid on a platform
        # that has none would push the probe down its POSIX branch and assert
        # against a fabricated condition, instead of the AttributeError path that
        # is the real Windows behaviour under test.
        if hasattr(mod.os, "getuid"):
            monkeypatch.setattr(mod.os, "getuid", lambda: 9_999_999)

        result = mod._run_probe_resident_debris(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.resident.debris"
        # No debris → PASS.
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

        # Create the stale git sentinel.
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
        # Detail should mention the path.
        assert "coordinator-service" in result.detail or str(sentinel) in result.detail, (
            f"Detail should mention the debris path, got: {result.detail!r}"
        )

    def test_debris_never_emits_degraded_on_any_path(self, tmp_path: Path) -> None:
        """Regression guard: DEGRADED is reserved for genuine hard failures.

        Debris is harmless-but-stale; DEGRADED would mislead callers into thinking
        the engine is in a genuinely degraded operational state.
        """
        mod = _require_module()

        # Test with both sentinel and None root to cover both branches.
        for root in [tmp_path, None]:
            if root is not None:
                # Create debris to exercise the found path.
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
        """Probe-authoring invariant: None root produces a parseable result, not a crash."""
        mod = _require_module()

        result = mod._run_probe_resident_debris(None)

        # With None root, the sentinel check is skipped but the uid socket check
        # still runs.  Result should be PASS (no debris found) or contain debris
        # info if a real socket dir exists.
        assert _is_parseable_probe_result(result), (
            "None claude_klabauter_root must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.resident.debris"
        assert result.status in {mod._PASS, mod._INFO, mod._BROKEN}, (
            f"Unexpected status {result.status!r}; must be PASS, INFO, or BROKEN (never DEGRADED)"
        )
        assert result.status != mod._DEGRADED


# ---------------------------------------------------------------------------
# claude-klabauter.worktree.bloat tests
# ---------------------------------------------------------------------------


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
        """PASS when no files exceed the default 1 GiB threshold."""
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
        """A large file inside .git/ must NOT be flagged — the walk prunes .git/."""
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
        """None root produces a parseable PASS/skip result, not a crash."""
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


# ---------------------------------------------------------------------------
# claude-klabauter.version.sanity tests
# ---------------------------------------------------------------------------


class TestVersionSanityProbe:
    """_run_probe_version_sanity() — healthy path on a working repo.

    Version sanity checks three things: coordinator_core importable,
    _compute_core_version() resolves, coordinator_core.client is absent.
    On this healthy development tree, all three should succeed.
    """

    def test_version_sanity_healthy_repo_is_pass(self) -> None:
        """PASS on the development repo (coordinator_core intact, client retired)."""
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
        # Review: code-reviewer — F2: renamed from test_version_sanity_none_root_is_broken;
        # name said BROKEN but body explicitly accepts PASS (dev-tree import succeeds).
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
        """Probe-authoring invariant: result always has required fields, never a bare exception."""
        mod = _require_module()

        result = mod._run_probe_version_sanity(_REPO_ROOT)

        # The invariant: all five structural fields present.
        for attr in ("probe", "status", "detail", "remediation", "required"):
            assert hasattr(result, attr), (
                f"_ProbeResult missing required attribute {attr!r}"
            )
        assert result.probe == "claude-klabauter.version.sanity"
        assert isinstance(result.status, str) and len(result.status) > 0


# ---------------------------------------------------------------------------
# claude-klabauter.invoke.smoke tests
# ---------------------------------------------------------------------------


class TestInvokeSmokeProbe:
    """_run_probe_invoke_smoke() — success + graceful-skip-on-spawn-failure paths.

    Key invariant (probe-authoring invariant): the probe must ALWAYS emit a parseable
    _ProbeResult, never a bare crash — including on spawn failure.

    required=False is the other key: a spawn failure must yield SKIP or BROKEN
    (with required=False), not a hard BROKEN that drags the overall verdict down.
    """

    def test_invoke_smoke_healthy_repo_emits_parseable_result(self) -> None:
        """Probe emits a parseable _ProbeResult on the healthy development repo.

        Status is PASS (if coordinator_core.invoke ping works) or BROKEN
        (if the invoke path has a problem) — either is a valid parseable result.
        The test verifies the invariant; not the specific verdict.
        """
        mod = _require_module()

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "invoke smoke probe must always return a parseable _ProbeResult"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        # Review: code-reviewer — F9: removed INFO from valid set; INFO means skipped=True
        # which must not happen on a healthy repo with a valid root (would be a probe bug).
        assert result.status in {mod._PASS, mod._BROKEN}, (
            f"Unexpected status {result.status!r}; expected PASS or BROKEN on a healthy repo "
            "(INFO would indicate an unexpected skip, which is a probe bug)"
        )
        assert result.skipped is False, (
            "skipped=True must not fire on a healthy repo with a valid CLAUDE_KLABAUTER_ROOT"
        )
        # required=False is mandatory — spawn failure must not hold overall to BROKEN.
        assert result.required is False, (
            "claude-klabauter.invoke.smoke must always carry required=False (OPTIONAL probe)"
        )

    def test_invoke_smoke_spawn_failure_emits_skip_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKIP (not a crash) when subprocess.run raises FileNotFoundError (interpreter absent).

        Probe-authoring invariant: an optional probe's own spawn failure must emit a
        SKIP verdict envelope, never an unhandled exception.
        """
        mod = _require_module()

        def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError("no such interpreter")

        monkeypatch.setattr(mod.subprocess, "run", _raise_fnf)

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "spawn FileNotFoundError must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        # On spawn failure the probe emits INFO with skipped=True (SKIP envelope).
        # Review: code-reviewer — F8: was {INFO, PASS}; PASS is unreachable when skipped=True
        # (probe always sets status=_INFO on the skip path); pinned to INFO to match impl.
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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BROKEN (not a crash) when subprocess.run raises TimeoutExpired.

        A timeout is a genuine spawn-path failure that warrants investigation,
        hence BROKEN rather than SKIP.  Still required=False and parseable.
        """
        mod = _require_module()
        import subprocess as _subprocess

        def _raise_timeout(*args, **kwargs):
            raise _subprocess.TimeoutExpired(cmd=args[0] if args else "cmd", timeout=30)

        monkeypatch.setattr(mod.subprocess, "run", _raise_timeout)

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "TimeoutExpired must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._BROKEN, (
            f"Expected BROKEN on TimeoutExpired, got {result.status!r}"
        )
        assert result.required is False

    def test_invoke_smoke_none_root_emits_skip(self) -> None:
        """SKIP (not a crash) when claude_klabauter_root is None.

        Probe-authoring invariant: None root must produce a parseable result.
        """
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
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BROKEN (not a crash) when the invoke subprocess exits non-zero.

        A non-zero exit is a real dispatch failure — BROKEN with required=False
        so the overall verdict degrades gracefully rather than crashing.
        """
        mod = _require_module()

        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "invoke crashed"

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

        assert _is_parseable_probe_result(result), (
            "non-zero exit must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._BROKEN, (
            f"Expected BROKEN on non-zero invoke exit, got {result.status!r}"
        )
        assert result.required is False

    def test_invoke_smoke_exit_zero_malformed_stdout_is_broken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BROKEN (not a crash) when subprocess exits 0 but stdout is not valid JSON.

        Probe-authoring invariant: the probe must survive a JSON parse failure on the
        invoke response (e.g. an import-time warning that pollutes stdout) and emit a
        parseable _ProbeResult rather than propagating the parse exception.
        # Review: code-reviewer — F7: new test for exit-0 + non-parseable stdout path.
        """
        mod = _require_module()

        class _FakeResult:
            returncode = 0
            stdout = "not json {{{"
            stderr = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: _FakeResult())

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

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
        self, monkeypatch: pytest.MonkeyPatch
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

        result = mod._run_probe_invoke_smoke(_REPO_ROOT)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.invoke.smoke"
        assert result.status == mod._PASS, (
            f"Expected PASS when invoke returns ok=true, got {result.status!r}"
        )
        assert result.required is False
        # Verify the detail says "CAN dispatch" not "IS connected" (scope discipline).
        assert "CAN dispatch" in result.detail or "can dispatch" in result.detail.lower(), (
            f"Probe detail must state the entrypoint CAN dispatch (scope discipline); "
            f"got: {result.detail!r}"
        )


# ---------------------------------------------------------------------------
# claude-klabauter.execnet.orphaned_gateways tests
# ---------------------------------------------------------------------------
#
# Fakes the process table entirely (no real process spawned — this machine runs
# 50-70 concurrent LLM sessions; a spawning test here would be load-hostile) by
# injecting a fake `psutil` module into sys.modules before the probe's own
# lazy, guarded `import psutil` resolves it (see the probe's own docstring for
# why that import is probe-local rather than module-top-level).


class _FakeProc:
    """Stand-in for a psutil.Process yielded by process_iter(attrs)."""

    def __init__(self, info: dict) -> None:
        self.info = info


def _make_fake_psutil(procs, alive_pids):
    """Build a minimal fake psutil module exposing only what the probe calls.

    process_iter(attrs) ignores attrs (the real API narrows fields fetched;
    the fakes here already carry only pid/ppid/cmdline) and yields _FakeProc
    instances built from *procs*. pid_exists(pid) reports True iff pid is in
    *alive_pids* — the sole primitive the probe uses to test controller
    liveness, deliberately excluding pid 1/0 so the reparented-to-init branch
    is exercised via the probe's own special-case, not via this fake.
    """
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
    """_run_probe_orphaned_execnet_gateways() — AC4 is the load-bearing case.

    AC4 (docs/plans/2026-08-13-reap-orphaned-execnet-gateways.md): a gateway
    process matching the execnet signature under a LIVE controller is a
    healthy in-flight test run and MUST be PASS — observed live during the
    spike (four such gateways under a peer's running suite), not
    hypothetical. Every test here fakes psutil; none spawns a real process.
    """

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
        # alive_pids includes 1 to prove the probe special-cases ppid in (0, 1)
        # rather than trusting pid_exists(1)'s trivially-true answer.
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
        """PASS when a gateway's controller is alive — AC4's central case.

        This is the exact false-positive shape observed live during the spike:
        a raw signature count would call this a leak. The probe must not.
        """
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
        """PASS when no process on the box carries the execnet signature."""
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
        """Only the dead-controller gateway is flagged when both kinds are present."""
        mod = _require_module()

        procs = [
            {"pid": 100, "ppid": 200, "cmdline": _GATEWAY_CMDLINE},   # live controller
            {"pid": 101, "ppid": 999, "cmdline": _GATEWAY_CMDLINE},   # dead controller
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
        """SKIP (not a crash) when psutil is not importable.

        Probe-authoring invariant: an optional probe's missing soft-dependency
        must emit a SKIP envelope, never an unhandled exception.
        """
        mod = _require_module()

        monkeypatch.setitem(sys.modules, "psutil", None)  # forces ImportError on import

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
        """An exception from pid_exists() must NOT be treated as a confirmed orphan.

        Fail-closed-to-"assume alive" mirrors the repo's existing liveness
        posture (coordinator_core.ops.session.reap's fail-closed-to-keep) —
        an indeterminate read is not evidence of a dead controller.
        """
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
