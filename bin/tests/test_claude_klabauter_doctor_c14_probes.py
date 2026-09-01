"""
bin.tests.test_claude_klabauter_doctor_c14_probes — Unit tests for the C14 Windows-portability
probes added to bin/claude-klabauter-doctor-probe.py.

Covers two probes introduced in C14, exercising healthy + fault paths via direct
function calls (no subprocess) — loads bin/claude-klabauter-doctor-probe.py as a module
via importlib for fast, isolated, monkeypatched execution.

Probes under test:
  claude-klabauter.root.pointer   — claude-klabauter-live-root pointer file present at
                          <settings-home>/machine-local/.claude-klabauter-live-root and matches the
                          resolved CLAUDE_KLABAUTER_ROOT; DEGRADED (not hard FAIL) on absence/mismatch.
  claude-klabauter.invoke.latency — measures a single coordinator_core.invoke round-trip as PROCESS
                          TIME (never wall clock) against a 500 ms brightline budget;
                          DEGRADED (not BROKEN) over budget or on timeout.

Also covers the C4 "Question the sink cannot answer:" sentinel — every retained probe
in _IMPLEMENTED_IDS must carry the literal heading in its own docstring, so Clause B of
pln-2026-08-27-the-undeclared-harness-and-the-redundant-probes's exit criterion is
machine-checkable rather than a grep-for-a-phrase-nobody-commits-to.

Probe-authoring invariant (per state/lessons/2026-07-04-a-diagnostic-must-always-emit-a-parseabl.yaml):
  Every probe must emit a parseable _ProbeResult on ALL paths — including its own
  bootstrap failure.  A bare exception or empty result is the exact failure the doctor
  exists to prevent.  Tests assert this invariant explicitly on fault paths.

Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C14
"""

from __future__ import annotations

import ast
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
        (pointer_dir / ".claude-klabauter-live-root").write_text(str(claude_klabauter_root))

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
        (pointer_dir / ".claude-klabauter-live-root").write_text(str(other_root))

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
        (pointer_dir / ".claude-klabauter-live-root").write_text("/some/path")

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
    measurement is bounded (daemon thread + Thread.join(timeout=...)) so this probe
    can never hang the doctor.

    The probe measures via `coordinator_core.benchmarks.process_time
    .single_invocation_tree_process_time`, imported locally inside the probe function
    per call — so these tests monkeypatch that name on the real
    `coordinator_core.benchmarks.process_time` module (not `mod.subprocess`), which is
    picked up fresh by the probe's own local import each invocation.
    """

    @pytest.fixture
    def stamped_root(self, tmp_path: Path) -> Path:
        """A root the probe will actually measure against.

        Only a STAMPED engine root reaches the measurement arms (DR-331);
        `_REPO_ROOT` is a source clone whose dispatch is refused at the stamp
        gate, so pointing these cases there would time a refusal instead of a
        round-trip. Patching the predicate is not an option: `_require_module()`
        re-execs the probe file per call.
        """
        stamp = tmp_path / "coordinator_core" / "_engine_stamp"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("sha-published")
        return tmp_path

    @pytest.fixture
    def process_time_mod(self):
        from coordinator_core.benchmarks import process_time as ptm

        return ptm

    def test_latency_under_budget_is_pass(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path, process_time_mod
    ) -> None:
        """PASS when the (mocked) round-trip completes well under the process-time budget."""
        mod = _require_module()

        def _fake_measure(*a, **kw):
            return {
                "process_time_ms": 50.0,
                "wall_ms": 60.0,
                "procs": 1,
                "rc": 0,
                "k": 1,
                "stdout_path": kw.get("stdout_path"),
                "stderr_path": kw.get("stderr_path"),
            }

        monkeypatch.setattr(
            process_time_mod, "single_invocation_tree_process_time", _fake_measure
        )

        result = mod._run_probe_invoke_latency(stamped_root)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.status == mod._PASS, (
            f"Expected PASS for a fast mocked round-trip, got {result.status!r}"
        )
        assert result.required is False
        assert result.data is not None
        assert result.data["timed_out"] is False
        assert result.data["budget_ms"] == mod._INVOKE_LATENCY_BUDGET_MS
        assert result.data["budget_ms"] == 500, (
            "claude-klabauter.invoke.latency must gate against the 500 ms brightline, not 2000 ms"
        )
        assert result.data["process_time_ms"] == 50.0
        assert "wall_ms" not in result.data, (
            "the gated data must not smuggle a wall-clock figure back in under a "
            "different key"
        )

    def test_latency_over_budget_is_degraded(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path, process_time_mod
    ) -> None:
        """DEGRADED (not BROKEN) when the measured process time exceeds the budget."""
        mod = _require_module()

        def _fake_measure(*a, **kw):
            return {
                "process_time_ms": mod._INVOKE_LATENCY_BUDGET_MS + 100.0,
                "wall_ms": mod._INVOKE_LATENCY_BUDGET_MS + 120.0,
                "procs": 1,
                "rc": 0,
                "k": 1,
                "stdout_path": None,
                "stderr_path": None,
            }

        monkeypatch.setattr(
            process_time_mod, "single_invocation_tree_process_time", _fake_measure
        )

        result = mod._run_probe_invoke_latency(stamped_root)

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
        assert "claude-klabauter-live-root pointer" in result.remediation

    def test_latency_timeout_is_degraded_not_broken(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path, process_time_mod
    ) -> None:
        """DEGRADED (not BROKEN, not a hang) when the bounded measurement window elapses.

        A timeout IS the failure being detected — the probe must survive it and
        emit a DEGRADED verdict, never propagate the exception or hang itself.
        The bound is shrunk to keep the test fast; the fake measurement sleeps
        past it.
        """
        mod = _require_module()

        monkeypatch.setattr(mod, "_INVOKE_LATENCY_TIMEOUT_SECONDS", 0.1)

        def _hang_forever(*a, **kw):
            import time as _time

            _time.sleep(2.0)
            return {"process_time_ms": 1.0, "wall_ms": 1.0, "procs": 1, "rc": 0, "k": 1}

        monkeypatch.setattr(
            process_time_mod, "single_invocation_tree_process_time", _hang_forever
        )

        result = mod._run_probe_invoke_latency(stamped_root)

        assert _is_parseable_probe_result(result), (
            "a bounded-window timeout must produce a parseable _ProbeResult, not a crash"
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
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path, process_time_mod
    ) -> None:
        """SKIP (not a crash) when the measurement primitive raises FileNotFoundError
        (interpreter absent)."""
        mod = _require_module()

        def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError("no such interpreter")

        monkeypatch.setattr(
            process_time_mod, "single_invocation_tree_process_time", _raise_fnf
        )

        result = mod._run_probe_invoke_latency(stamped_root)

        assert _is_parseable_probe_result(result), (
            "spawn FileNotFoundError must produce a parseable _ProbeResult, not a crash"
        )
        assert result.probe == "claude-klabauter.invoke.latency"
        assert result.skipped is True
        assert result.required is False
        assert result.status == mod._INFO

    def test_latency_unsupported_platform_emits_skip_not_crash(
        self, monkeypatch: pytest.MonkeyPatch, stamped_root: Path, process_time_mod
    ) -> None:
        """SKIP (not BROKEN, not DEGRADED) when process time measurement itself is
        unavailable on this platform (`NotImplementedError`) — an unmeasurable
        platform is not the same fact as a slow round-trip."""
        mod = _require_module()

        def _raise_ni(*args, **kwargs):
            raise NotImplementedError("no process-time primitive on this platform")

        monkeypatch.setattr(
            process_time_mod, "single_invocation_tree_process_time", _raise_ni
        )

        result = mod._run_probe_invoke_latency(stamped_root)

        assert _is_parseable_probe_result(result)
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


class TestInvokeLatencyDispatchRoot:
    """The latency probe measures the same tree the smoke probe dispatches
    from -- a refusal at the stamp gate times the refusal, not the round-trip
    this budget is about (DR-331, DR-326)."""

    def test_unstamped_clone_measures_the_published_mirror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mod = _require_module()

        clone = tmp_path / "source-clone"
        clone.mkdir()
        mirror = tmp_path / "mirror"
        stamp = mirror / "coordinator_core" / "_engine_stamp"
        stamp.parent.mkdir(parents=True)
        stamp.write_text("sha-published")

        from coordinator_core import engine_root as engine_root_mod

        monkeypatch.setattr(
            engine_root_mod, "published_engine_mirror_path", lambda: str(mirror)
        )

        from coordinator_core.benchmarks import process_time as process_time_mod

        cwds: list[object] = []

        def _capture(*a, **kw):
            cwds.append(kw.get("cwd"))
            return {
                "process_time_ms": 50.0,
                "wall_ms": 60.0,
                "procs": 1,
                "rc": 0,
                "k": 1,
                "stdout_path": kw.get("stdout_path"),
                "stderr_path": kw.get("stderr_path"),
            }

        monkeypatch.setattr(
            process_time_mod, "single_invocation_tree_process_time", _capture
        )

        result = mod._run_probe_invoke_latency(clone)

        assert result.status == mod._PASS
        assert cwds == [str(mirror)]
        assert result.data["dispatch_root"] == str(mirror)

    def test_no_stamped_root_anywhere_is_inconclusive_without_spawning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        mod = _require_module()

        from coordinator_core import engine_root as engine_root_mod

        monkeypatch.setattr(
            engine_root_mod, "published_engine_mirror_path", lambda: None
        )

        spawned: list[object] = []
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: spawned.append(a))

        result = mod._run_probe_invoke_latency(tmp_path)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is False
        assert result.data["dispatch_root"] is None
        assert spawned == []


# ---------------------------------------------------------------------------
# C4 sentinel — "Question the sink cannot answer:" per retained probe
# ---------------------------------------------------------------------------

# The three probes RETAINED by pln-2026-08-27-the-undeclared-harness-and-the-
# redundant-probes § C4 — each MUST carry the literal heading
# "Question the sink cannot answer:" in its own docstring. This is the
# constant the plan's Clause B falsifies on: any id here without a matching
# docstring block fails the assertion below.
_IMPLEMENTED_IDS = {
    "claude-klabauter.invoke.smoke": "_run_probe_invoke_smoke",
    "claude-klabauter.invoke.latency": "_run_probe_invoke_latency",
    "claude-klabauter.warm.roundtrip": "_run_probe_warm_roundtrip",
}

_SENTINEL_HEADING = "Question the sink cannot answer:"


class TestQuestionTheSinkCannotAnswerSentinel:
    """Every retained probe in _IMPLEMENTED_IDS states, in its own docstring, the
    question it asks that the op-census sink cannot answer -- making disposition
    (RETAINED, and why) machine-checkable rather than a claim nobody has to prove."""

    def test_implemented_ids_are_a_subset_of_actual_probe_functions(self) -> None:
        """_IMPLEMENTED_IDS's function names must all exist as real `_run_probe_*`
        functions on the loaded module.

        This does not (and cannot, without inventing a derivable "retained"
        category — see state/debt-backlog/2026-08-28-the-origin-guard-stops-at-
        the-benchmarks-e72083a938e5.yaml) assert the dict is exhaustive over the
        probe population. It only catches the cheap, mechanical failure: a
        function named in the dict gets renamed or deleted and the parametrized
        test below silently stops covering anything (an AttributeError inside a
        fixture setup, not a red assertion on the sentinel itself).
        """
        mod = _require_module()

        actual_probe_fns = {
            name for name in dir(mod) if name.startswith("_run_probe_")
        }
        missing = set(_IMPLEMENTED_IDS.values()) - actual_probe_fns
        assert not missing, (
            f"_IMPLEMENTED_IDS names function(s) no longer present on the module: "
            f"{sorted(missing)!r} — renamed or deleted without updating the dict"
        )

    @pytest.mark.parametrize("probe_id,fn_name", sorted(_IMPLEMENTED_IDS.items()))
    def test_retained_probe_carries_sentinel_heading(
        self, probe_id: str, fn_name: str
    ) -> None:
        mod = _require_module()

        fn = getattr(mod, fn_name)
        doc = fn.__doc__ or ""

        assert probe_id in doc, (
            f"{fn_name}'s docstring must reference its own probe id {probe_id!r}"
        )
        assert _SENTINEL_HEADING in doc, (
            f"{fn_name} ({probe_id}) is RETAINED but its docstring is missing the "
            f"literal heading {_SENTINEL_HEADING!r} — every retained probe must "
            "state, in its own body, the question it asks that the dispatch sink "
            "cannot answer (pln-2026-08-27-the-undeclared-harness-and-the-"
            "redundant-probes § C4)."
        )

        # The prose following the heading must be non-trivial, not a bare label.
        #
        # Ceiling, stated honestly: this is a length floor only (> 40 chars),
        # not semantic enforcement. It blocks a bare label following the
        # heading, but does not check the prose is actually phrased as a
        # question, names a concrete reason the sink cannot answer it, or
        # differs from the probe's own one-line summary — a future edit could
        # satisfy this assertion with restated-behavior filler padded past the
        # floor. Reviewed 2026-08-28 (coordinator:code-reviewer,
        # coordinatorcode-reviewer.a293fb187d8013989): all three current
        # probes' prose is genuine, but that was confirmed by reading, not by
        # this test.
        after = doc.split(_SENTINEL_HEADING, 1)[1]
        assert len(after.strip()) > 40, (
            f"{fn_name} ({probe_id}): the sentinel heading must be followed by an "
            "actual question-and-why statement, not left empty"
        )


# ---------------------------------------------------------------------------
# Every invoke remediation names the tree it must be run from
# ---------------------------------------------------------------------------


def test_invoke_probe_remediations_name_the_dispatch_root() -> None:
    """No arm of either invoke probe may hand out a rootless `invoke ping`.

    `_resolve_dispatch_root` exists because the same command returns opposite
    answers from two trees: from the published mirror it is a 65 ms ok=true,
    from a source clone it is the DR-331 no-cold-fallback refusal. A
    remediation that omits the root therefore reproduces the ruling on the
    operator's own box and reads as a broken entrypoint — sending them to
    `ops/ping.py`, which is never the cause. Two arms already interpolated
    `dispatch_root` and two did not; nothing held the other two.

    Source-level, not behavioural: it holds arms added LATER, which a fixture
    per current arm would not.
    """
    mod = _require_module()

    source = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    targets = {"_run_probe_invoke_smoke", "_run_probe_invoke_latency"}
    rootless: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in targets:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for kw in call.keywords or []:
                if kw.arg != "remediation":
                    continue
                # Concatenated literals and f-strings alike: collect the
                # literal fragments, and separately whether `dispatch_root`
                # is interpolated anywhere in the same expression.
                literal = "".join(
                    n.value for n in ast.walk(kw.value)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                )
                if "coordinator_core.invoke" not in literal:
                    continue
                names = {
                    n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)
                }
                if "dispatch_root" not in names:
                    rootless.append((node.name, kw.value.lineno))

    assert not rootless, (
        "invoke remediation names `coordinator_core.invoke` without interpolating "
        f"`dispatch_root`: {rootless}. From a source clone that command returns the "
        "DR-331 refusal, not a verdict — name the tree the probe itself dispatched "
        "from (see _resolve_dispatch_root)."
    )
