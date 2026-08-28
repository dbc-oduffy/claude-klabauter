"""
coordinator_core.benchmarks.tests.test_spawn_helpers_declare_origin

Purpose: asserts C1d's chunk body — the in-repo spawn helpers
(``timer.py::time_invocation``, ``process_time.py::batched_process_time_ms``,
``process_time.py::single_invocation_tree_process_time``) tag the CHILD they
spawn with a benchmark origin, outside any measured span, so helper-routed
subject traffic is tagged BENCHMARK regardless of whether the guard suite
(C1) ever runs -- WITHOUT mutating the calling process's own
``os.environ`` (C1d, fixing the defect C1b introduced: ``declare_
benchmark_origin()`` writes ORIGIN_ENV into the interpreter-global
``os.environ``, correct at a driver's own entry but wrong inside a library
spawn helper any caller may invoke mid-process).

Three legs per helper:
  1. Presence: the helper's source builds a child-scoped env carrying the
     benchmark origin (``_child_env_with_benchmark_origin`` /
     ``_env_with_benchmark_origin``), never calling
     ``declare_benchmark_origin()`` itself (that would still mutate the
     caller's own ``os.environ``).
  2. Placement: the env build sits BEFORE the first ``time.perf_counter()``
     (or platform dispatch that itself starts timing) — never inside a
     measured span, per C1b's hard placement constraint, which still binds.
  3. No-leak: calling each helper leaves the CALLING process's own
     ``os.environ`` unchanged, while the spawned child still records
     origin=benchmark (this chunk's own reproduction fixture).

Source-inspection based for legs 1-2 (not behavioral) because the
underlying platform paths (Windows job objects, Darwin kqueue) are not
portably exercisable in a unit test, and the placement property itself is
structural. Leg 3 is behavioral -- it is the property this chunk exists to
prove.

Negative-spec: does NOT assert any env-declaring call happens at IMPORT
time anywhere in these modules -- that would violate declare_benchmark_
origin's own negative-spec (module docstring in
coordinator_core/benchmarks/__init__.py), which still binds even though
these helpers no longer call that function directly.

Spec backlink: state/dispatch-briefs/2026-08-27-the-undeclared-harness-and-the-redundant-probes/C1d.md
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks import process_time, timer
from coordinator_core.telemetry import op_latency


def _source_lines(func) -> list[str]:
    src = inspect.getsource(func)
    return src.splitlines()


def _find_call_line(lines: list[str], needle: str) -> int:
    """Returns the 0-based index of the first line containing `needle`,
    or -1 if absent."""
    for i, line in enumerate(lines):
        if needle in line:
            return i
    return -1


class TestTimeInvocationDeclaresOrigin:
    def test_builds_child_scoped_origin_env(self):
        lines = _source_lines(timer.time_invocation)
        assert _find_call_line(lines, "_child_env_with_benchmark_origin()") != -1, (
            "time_invocation must build a child-scoped env carrying the "
            "benchmark origin at its own spawn call"
        )
        assert _find_call_line(lines, "declare_benchmark_origin()") == -1, (
            "time_invocation must not call declare_benchmark_origin() -- "
            "that mutates the caller's own os.environ (C1d)"
        )

    def test_declare_precedes_measured_span(self):
        lines = _source_lines(timer.time_invocation)
        declare_idx = _find_call_line(lines, "_child_env_with_benchmark_origin()")
        span_idx = _find_call_line(lines, "time.perf_counter()")
        assert declare_idx != -1 and span_idx != -1
        assert declare_idx < span_idx, (
            "the child-scoped origin env must be built BEFORE the "
            "perf_counter() span opens -- building it inside the measured "
            "region charges its own cost to the benchmark figure"
        )


class TestBatchedProcessTimeDeclaresOrigin:
    def test_builds_child_scoped_origin_env(self):
        lines = _source_lines(process_time.batched_process_time_ms)
        assert _find_call_line(lines, "_env_with_benchmark_origin(") != -1, (
            "batched_process_time_ms must build a child-scoped env "
            "carrying the benchmark origin at its own spawn call"
        )
        assert _find_call_line(lines, "declare_benchmark_origin()") == -1, (
            "batched_process_time_ms must not call declare_benchmark_origin() "
            "-- that mutates the caller's own os.environ (C1d)"
        )

    def test_declare_precedes_platform_dispatch(self):
        lines = _source_lines(process_time.batched_process_time_ms)
        declare_idx = _find_call_line(lines, "_env_with_benchmark_origin(")
        windows_idx = _find_call_line(lines, "_windows_batched_process_time_ms(")
        darwin_idx = _find_call_line(lines, "_darwin_batched_process_time_ms(")
        assert declare_idx != -1
        assert windows_idx != -1 and darwin_idx != -1
        assert declare_idx < windows_idx, (
            "the child-scoped origin env must precede the Windows platform "
            "dispatch, which opens its own measured span"
        )
        assert declare_idx < darwin_idx, (
            "the child-scoped origin env must precede the Darwin platform "
            "dispatch, which opens its own measured span"
        )


class TestSingleInvocationTreeProcessTimeDeclaresOrigin:
    def test_builds_child_scoped_origin_env(self):
        lines = _source_lines(process_time.single_invocation_tree_process_time)
        assert _find_call_line(lines, "_env_with_benchmark_origin(") != -1, (
            "single_invocation_tree_process_time must build a child-scoped "
            "env carrying the benchmark origin at its own spawn call"
        )
        assert _find_call_line(lines, "declare_benchmark_origin()") == -1, (
            "single_invocation_tree_process_time must not call "
            "declare_benchmark_origin() -- that mutates the caller's own "
            "os.environ (C1d)"
        )

    def test_declare_precedes_measured_span(self):
        lines = _source_lines(process_time.single_invocation_tree_process_time)
        declare_idx = _find_call_line(lines, "_env_with_benchmark_origin(")
        span_idx = _find_call_line(lines, "time.perf_counter()")
        assert declare_idx != -1 and span_idx != -1
        assert declare_idx < span_idx, (
            "the child-scoped origin env must be built BEFORE the first "
            "perf_counter() span opens -- building it inside a measured "
            "region contaminates the figure"
        )


class TestSpawnHelpersDoNotLeakOriginIntoCallerEnviron:
    """C1d's own reproduction fixture: calling each helper must leave the
    CALLING process's os.environ unchanged, while the spawned child still
    records origin=benchmark. This is what separates a driver's own entry
    (where the global write is correct) from a library spawn helper mid-
    process (where it is not)."""

    def test_time_invocation_does_not_leak(self, monkeypatch):
        monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
        before = dict(os.environ)

        elapsed = timer.time_invocation("ping", "{}", repo=None)

        assert elapsed >= 0.0
        assert op_latency.ORIGIN_ENV not in os.environ, (
            "time_invocation leaked ORIGIN_ENV into the caller's own "
            "os.environ"
        )
        assert dict(os.environ) == before, (
            "time_invocation mutated the caller's own os.environ"
        )

    def test_batched_process_time_ms_does_not_leak(self, monkeypatch):
        monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
        if not (process_time.IS_WINDOWS or process_time.IS_DARWIN):
            pytest.skip("batched_process_time_ms has no primitive off Windows/Darwin")
        before = dict(os.environ)

        process_time.batched_process_time_ms(
            [sys.executable, "-c", "pass"], k=1
        )

        assert op_latency.ORIGIN_ENV not in os.environ, (
            "batched_process_time_ms leaked ORIGIN_ENV into the caller's "
            "own os.environ"
        )
        assert dict(os.environ) == before, (
            "batched_process_time_ms mutated the caller's own os.environ"
        )

    def test_single_invocation_tree_process_time_does_not_leak(self, monkeypatch):
        monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
        if not (process_time.IS_WINDOWS or process_time.IS_DARWIN):
            pytest.skip(
                "single_invocation_tree_process_time has no primitive off "
                "Windows/Darwin"
            )
        before = dict(os.environ)

        process_time.single_invocation_tree_process_time(
            [sys.executable, "-c", "pass"]
        )

        assert op_latency.ORIGIN_ENV not in os.environ, (
            "single_invocation_tree_process_time leaked ORIGIN_ENV into "
            "the caller's own os.environ"
        )
        assert dict(os.environ) == before, (
            "single_invocation_tree_process_time mutated the caller's own "
            "os.environ"
        )


class TestSpawnHelpersTagTheChildEnv:
    """Review: code-reviewer (Slice B, P2, item 5) -- legs 1-2 above are
    source-text/AST assertions and leg 3 (no-leak) only proves the CALLING
    process's os.environ is untouched. Neither proves the CHILD actually
    receives ORIGIN_ENV=BENCHMARK -- a rewrite that keeps the right-named
    helper call but silently drops the `env=` kwarg at the real
    subprocess.run/Popen spawn site would still pass every one of those.

    Chosen mechanism: wrap the module's own `subprocess.run`/`subprocess.Popen`
    to capture the `env=` kwarg actually handed to the real spawn call, while
    still delegating to the real implementation so the child genuinely spawns
    (this is not a mock that replaces spawning -- it observes it). This was
    chosen over asserting against the on-disk op-latency sink because a bare
    `python -m coordinator_core.invoke` child on this clone refuses to
    dispatch without a build stamp (DR-315) -- `--allow-unstamped-dispatch` is
    the documented opt-out and `timer.py::_build_argv` already carries it, but
    routing through the durable sink would make this test depend on sink
    location/rotation as well as the origin tag, which is more than this
    property needs. Capturing the real `env=` kwarg is still fully
    behavioral -- it fails exactly when the leak this class exists to catch
    (right-named call, dropped kwarg) happens -- without that extra coupling.
    """

    def test_time_invocation_env_reaches_subprocess_run(self, monkeypatch):
        monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
        captured = {}
        real_run = timer.subprocess.run

        def capturing_run(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return real_run(*args, **kwargs)

        monkeypatch.setattr(timer.subprocess, "run", capturing_run)

        timer.time_invocation("ping", "{}", repo=None)

        assert captured.get("env") is not None, (
            "time_invocation must pass env= to subprocess.run"
        )
        assert captured["env"].get(op_latency.ORIGIN_ENV) == op_latency.BENCHMARK, (
            "the child's env must actually carry ORIGIN_ENV=BENCHMARK at the "
            "real subprocess.run call, not merely via a correctly-named "
            "builder function"
        )

    @pytest.mark.skipif(
        not process_time.IS_WINDOWS, reason="Windows-only spawn primitive"
    )
    def test_batched_process_time_ms_env_reaches_popen(self, monkeypatch):
        monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
        captured = {}
        real_popen = process_time.subprocess.Popen

        def capturing_popen(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(process_time.subprocess, "Popen", capturing_popen)

        process_time.batched_process_time_ms([sys.executable, "-c", "pass"], k=1)

        assert captured.get("env") is not None, (
            "batched_process_time_ms must pass env= to the real Popen call"
        )
        assert captured["env"].get(op_latency.ORIGIN_ENV) == op_latency.BENCHMARK, (
            "the child's env must actually carry ORIGIN_ENV=BENCHMARK at the "
            "real Popen call, not merely via a correctly-named builder "
            "function"
        )

    @pytest.mark.skipif(
        not process_time.IS_WINDOWS, reason="Windows-only spawn primitive"
    )
    def test_single_invocation_tree_process_time_env_reaches_popen(self, monkeypatch):
        monkeypatch.delenv(op_latency.ORIGIN_ENV, raising=False)
        captured = {}
        real_popen = process_time.subprocess.Popen

        def capturing_popen(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(process_time.subprocess, "Popen", capturing_popen)

        process_time.single_invocation_tree_process_time(
            [sys.executable, "-c", "pass"]
        )

        assert captured.get("env") is not None, (
            "single_invocation_tree_process_time must pass env= to the real "
            "Popen call"
        )
        assert captured["env"].get(op_latency.ORIGIN_ENV) == op_latency.BENCHMARK, (
            "the child's env must actually carry ORIGIN_ENV=BENCHMARK at the "
            "real Popen call, not merely via a correctly-named builder "
            "function"
        )


class TestNoModuleLevelDeclare:
    """Negative-spec: declare must never be an import-time side effect."""

    @pytest.mark.parametrize("mod", [timer, process_time])
    def test_no_module_level_declare_call(self, mod):
        path = Path(inspect.getfile(mod))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            # Any Call expression at module top level naming
            # declare_benchmark_origin would be an import-time side effect.
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                assert name != "declare_benchmark_origin", (
                    f"{mod.__name__} calls declare_benchmark_origin() at "
                    "module level -- this violates its own negative-spec "
                    "(tags any process that merely imports the module)"
                )
