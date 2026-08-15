"""
coordinator_core.benchmarks.composition_probe -- one-shot, non-mutating
wall-clock instrument for a real PARTITION-MANDATORY WSC-close computation.

Purpose: docs/plans/2026-08-15-composition-invocation-budgets.md C1. Two
prior attributions of the measured 1726s incident were wrong ("~340 engine
spawns x ~5s" -- static engine-spawn count on this path is 4; "~3,700 git
spawns at ~39ms" -- that is only ~145s). This module names the third,
dominant, previously-unnamed term by wall-clock attribution into four
buckets over ONE real close computation:

    (a) git subprocess time      -- every `git ...` child this path spawns
    (b) engine subprocess time   -- the `cc_invoke.route("coverage.gate")`
                                     child (review-coverage-gate.py, --no-mint)
    (c) in-process corpus I/O    -- the on-disk trail-record load plus the
                                     THREE consumer passes over it
                                     (chain_partition_uncovered_shas,
                                     chain_partition_execution_basis_report,
                                     _resolve_broadly_reviewed_shas), each
                                     minus whatever git time (a) occurred
                                     inside that consumer's own call
    (d) unattributed residue     -- total measured wall clock minus (a),
                                     (b), (c). C1's own exit condition:
                                     NOT complete while this exceeds 20%.

NON-MUTATING BY CONSTRUCTION. This module never calls `cmd_brightline_gate`
(coordinator/bin/wsc-coverage-gate-runner.py) -- that function unconditionally
calls `_persist_brightline_verdict`, which WRITES a verdict record under
state/ceremony/wsc-chain-partition-verdict/ keyed by the resolved session id.
On a shared tree with a dozen-plus concurrent sessions that write is not
safe to trigger from a measurement harness. Instead this module drives the
same PARTITION-MANDATORY code path `cmd_brightline_gate` drives (lines
~1944-2084 of that file, same call order, same arguments) by calling its
already-isolated, read-only resolver functions directly:
`_derive_dag_shas`, `_resolve_chain_code_shas`, `_resolve_chain_dag_shas`,
`_resolve_chain_planning_shas`, `_load_trail_records`,
`directives_review.chain_partition_uncovered_shas`,
`directives_review.chain_partition_execution_basis_report`,
`_resolve_broadly_reviewed_shas` -- plus, for bucket (b),
`_run_review_coverage_gate(from_handoff, mint_chain_waivers=False)`, the
documented "--no-mint ... for wall-clock/dry measurement callers that must
not mutate state" seam that function's own docstring names for exactly this
use. None of these write to disk; review-coverage-gate.py's only side
effect (the waiver mint) is gated off by `mint_chain_waivers=False`.

Not a profiler to keep -- a measurement harness for C1, run ad hoc via
`__main__`. cProfile is used only as the C1-mandated fallback when bucket
(d) exceeds 20%, to identify what to name next; its output is not part of
this module's steady-state contract.
"""

from __future__ import annotations

import cProfile
import importlib.util
import io
import pstats
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER_PATH = _REPO_ROOT / "coordinator" / "bin" / "wsc-coverage-gate-runner.py"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.workstream_complete import directives_review  # noqa: E402
from coordinator_core.benchmarks import ambient_sampler  # noqa: E402


def _load_runner_module():
    """Load coordinator/bin/wsc-coverage-gate-runner.py by file path -- it is
    a hyphenated bin script, not an importable package (same seam
    coordinator/bin/tests/test_wsc_coverage_gate_runner.py's own
    `_load_module` uses, and `_resolve_closing_session_id`'s own docstring
    names as the correct one for this file)."""
    spec = importlib.util.spec_from_file_location(
        "wsc_coverage_gate_runner_probe", _RUNNER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"composition_probe: could not resolve a module spec for {_RUNNER_PATH} "
            "-- the path does not exist or is not a loadable Python file."
        )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _SpawnLedger:
    """Records (kind, argv0, elapsed_s) for every subprocess.run call made
    anywhere during the measured window -- git and engine calls alike are
    routed through the stdlib `subprocess.run` symbol by every call site in
    wsc-coverage-gate-runner.py and its git-runner helpers, so patching that
    one attribute catches all of them regardless of which module issued the
    call."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def git_total(self) -> float:
        return sum(e["elapsed_s"] for e in self.entries if e["kind"] == "git")

    def engine_total(self) -> float:
        return sum(e["elapsed_s"] for e in self.entries if e["kind"] == "engine")

    def git_count(self) -> int:
        return sum(1 for e in self.entries if e["kind"] == "git")

    def engine_count(self) -> int:
        return sum(1 for e in self.entries if e["kind"] == "engine")


def _classify(cmd: list) -> str:
    if not cmd:
        return "other"
    head = str(cmd[0])
    if head == "git" or head.endswith("\\git.exe") or head.endswith("/git"):
        return "git"
    if head == sys.executable or head.lower().endswith("python.exe") or head.lower().endswith("python3"):
        return "engine"
    return "other"


def _patched_subprocess_run(ledger: _SpawnLedger, real_run):
    def _run(cmd, *args, **kwargs):
        t0 = time.perf_counter()
        result = real_run(cmd, *args, **kwargs)
        dt = time.perf_counter() - t0
        ledger.entries.append({
            "kind": _classify(list(cmd) if not isinstance(cmd, str) else [cmd]),
            "argv": list(cmd) if not isinstance(cmd, str) else [cmd],
            "elapsed_s": dt,
        })
        return result
    return _run


class SampleResult:
    """One PARTITION-MANDATORY-branch measurement over a single `from_handoff`."""

    def __init__(self, from_handoff: str) -> None:
        self.from_handoff = from_handoff
        self.total_wall_s = 0.0
        self.git_total_s = 0.0
        self.git_count = 0
        self.engine_total_s = 0.0
        self.engine_count = 0
        self.corpus_own_s = 0.0
        self.residue_s = 0.0
        self.residue_pct = 0.0
        self.trail_record_count = 0
        self.chain_dag_sha_count = 0
        self.chain_code_sha_count = 0
        self.uncovered_sha_count = 0
        self.reached_partition_mandatory = False
        self.dag_resolution_failed: bool | None = None
        self.ambient_before: dict | None = None
        self.ambient_after: dict | None = None
        self.profile_top: list[str] | None = None


def run_one_sample(from_handoff: str, *, profile: bool = False) -> SampleResult:
    """Drive the exact PARTITION-MANDATORY call sequence
    `cmd_brightline_gate` runs (coordinator/bin/wsc-coverage-gate-runner.py,
    lines ~1944-2084), through the module's own read-only resolvers, timing
    every subprocess spawn and every corpus-consumer call. Never calls
    `cmd_brightline_gate` itself (see module docstring -- that path writes a
    verdict record). Returns None-safe partial results on any resolution
    failure rather than raising -- a failed sample is reported as such, not
    silently retried with different inputs."""
    mod = _load_runner_module()
    result = SampleResult(from_handoff)
    ledger = _SpawnLedger()
    real_run = subprocess.run

    result.ambient_before = ambient_sampler.take_sample()

    profiler = cProfile.Profile() if profile else None

    with mock.patch("subprocess.run", _patched_subprocess_run(ledger, real_run)):
        t_total_0 = time.perf_counter()
        if profiler:
            profiler.enable()

        # (b) engine subprocess -- the documented non-mutating dry path.
        mod._run_review_coverage_gate(from_handoff, mint_chain_waivers=False)

        # Mirrors cmd_brightline_gate's own `dag_resolution_failed` gate --
        # `_resolve_chain_code_shas`/`_resolve_chain_dag_shas` already call
        # `_derive_dag_shas` themselves (same `_DAG_SHAS_CACHE` memo, so this
        # is not a second walk), but the production call site branches on
        # this result FIRST (dag_resolved is None => "diagnostics
        # unavailable" HALT, never reaching the uncovered-shas computation
        # this probe measures) -- recording it here keeps a failed-DAG
        # sample distinguishable from a genuine zero-uncovered discharge.
        dag_resolved = mod._derive_dag_shas(from_handoff)
        result.dag_resolution_failed = dag_resolved is None
        chain_code_shas = mod._resolve_chain_code_shas(from_handoff)
        chain_dag_shas = mod._resolve_chain_dag_shas(from_handoff)
        chain_planning_shas = mod._resolve_chain_planning_shas(from_handoff)

        t_load_0 = time.perf_counter()
        trail_records = mod._load_trail_records()
        t_load = time.perf_counter() - t_load_0

        result.trail_record_count = len(trail_records)
        result.chain_dag_sha_count = len(chain_dag_shas)
        result.chain_code_sha_count = len(chain_code_shas)

        corpus_own_s = t_load  # _load_trail_records has no subprocess of its own

        if chain_code_shas and chain_dag_shas:
            result.reached_partition_mandatory = True

            git_before = ledger.git_total()
            t0 = time.perf_counter()
            uncovered = directives_review.chain_partition_uncovered_shas(
                trail_records, chain_code_shas, chain_dag_shas, mod._resolve_range_shas,
                narrow_foreign_shas=mod._resolve_foreign_session_shas,
                vouched_shas=mod._resolve_vouched_shas,
                chain_planning_shas=chain_planning_shas,
            )
            t_uncovered = time.perf_counter() - t0
            corpus_own_s += t_uncovered - (ledger.git_total() - git_before)
            result.uncovered_sha_count = len(uncovered)

            git_before = ledger.git_total()
            t0 = time.perf_counter()
            try:
                directives_review.chain_partition_execution_basis_report(
                    trail_records, chain_code_shas, chain_dag_shas, mod._resolve_range_shas,
                    narrow_foreign_shas=mod._resolve_foreign_session_shas,
                    vouched_shas=mod._resolve_vouched_shas,
                    chain_planning_shas=chain_planning_shas,
                )
            except Exception:
                pass
            t_basis = time.perf_counter() - t0
            corpus_own_s += t_basis - (ledger.git_total() - git_before)

            if uncovered:
                git_before = ledger.git_total()
                t0 = time.perf_counter()
                mod._resolve_broadly_reviewed_shas(
                    trail_records, chain_code_shas, chain_dag_shas, chain_planning_shas,
                )
                t_broad = time.perf_counter() - t0
                corpus_own_s += t_broad - (ledger.git_total() - git_before)

        t_total = time.perf_counter() - t_total_0
        if profiler:
            profiler.disable()

    result.ambient_after = ambient_sampler.take_sample()
    result.total_wall_s = t_total
    result.git_total_s = ledger.git_total()
    result.git_count = ledger.git_count()
    result.engine_total_s = ledger.engine_total()
    result.engine_count = ledger.engine_count()
    result.corpus_own_s = corpus_own_s
    result.residue_s = max(
        0.0, t_total - result.git_total_s - result.engine_total_s - result.corpus_own_s,
    )
    result.residue_pct = (result.residue_s / t_total * 100.0) if t_total > 0 else 0.0

    if profiler:
        buf = io.StringIO()
        stats = pstats.Stats(profiler, stream=buf).sort_stats("cumulative")
        stats.print_stats(25)
        result.profile_top = buf.getvalue().splitlines()

    return result


if __name__ == "__main__":
    handoffs = sys.argv[1:] or [
        "state/handoffs/2026-08-15_205649_warm-engine-retires-the-per-invocation-cold-start.md",
        "state/handoffs/2026-08-15-fleet-wide-aggregate-invocation-budgets.md",
    ]
    for h in handoffs:
        r = run_one_sample(h, profile=True)
        print(f"--- {h} ---")
        print(f"reached_partition_mandatory={r.reached_partition_mandatory}")
        print(f"total_wall_s={r.total_wall_s:.3f}")
        print(f"git: count={r.git_count} total_s={r.git_total_s:.3f}")
        print(f"engine: count={r.engine_count} total_s={r.engine_total_s:.3f}")
        print(f"corpus_own_s={r.corpus_own_s:.3f}")
        print(f"residue_s={r.residue_s:.3f} residue_pct={r.residue_pct:.1f}%")
        print(f"trail_record_count={r.trail_record_count}")
        print(f"chain_dag_sha_count={r.chain_dag_sha_count} chain_code_sha_count={r.chain_code_sha_count} uncovered_sha_count={r.uncovered_sha_count}")
        print(f"ambient_before={r.ambient_before}")
        print(f"ambient_after={r.ambient_after}")
        if r.profile_top:
            print("\n".join(r.profile_top))
