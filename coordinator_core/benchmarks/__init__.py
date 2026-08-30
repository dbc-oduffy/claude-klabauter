"""
coordinator_core.benchmarks -- Per-op end-to-end latency benchmark harness.

Purpose: measures caller-experienced spawn-to-exit latency of the
`invoke` CLI entrypoint (see coordinator_core.invoke.__main__ for the exact
command form) under DR-215's command-type spawn-per-call execution model,
gates measured distributions against a two-level (OpClass-tier default +
per-op override) budget manifest, and persists an append-only
code_sha-keyed baseline store.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10

Measured-window discipline: a benchmark module's own git spawns (fixture
build, sample listing, post-window verification) are never the measured
subject — the timed subject is measured separately (`LiveTreeAccountant`,
`batched_process_time_ms`, `single_invocation_tree_process_time`, or
equivalent) around its own calls, outside any seam these fixture spawns
route through. Routing fixture-only git calls through
`coordinator_core.git.run` (G7) therefore adds no seam cost to the figure a
module reports. Individual call sites cite this paragraph rather than
restating it.
"""

from __future__ import annotations


def declare_benchmark_origin() -> None:
    """Stamp this process — and every child it spawns — as benchmark traffic.

    Sets ``op_latency.ORIGIN_ENV`` in ``os.environ`` so the op-latency sink tags
    every row this harness produces (and every row produced by the ``invoke``
    subprocesses it spawns, which inherit the environment) as ``BENCHMARK``
    rather than production traffic.

    Why it is set in the environment and not passed as an argument: the rows are
    written by the CHILD process that executes the op, several call frames and a
    process boundary away from the harness that asked for the measurement. There
    is no parameter path from here to there. Environment inheritance is the only
    channel that reaches the writer.

    Idempotent, and deliberately does NOT overwrite an existing declaration — a
    caller that has already declared a more specific origin keeps it.

    Negative-spec: never call this from library code, only from a benchmark
    driver's own entry. Setting it in a module import would tag any process that
    merely reads a baseline file, which silently deletes real traffic from the
    census — the exact failure this field exists to end, inverted.

    Imports are function-local by choice, not by a hot-path constraint like
    `ipc.py`'s (this module carries no negative-spec against top-level
    imports): it keeps `coordinator_core.benchmarks`'s own import surface
    minimal for callers that only need the rest of the package.
    """
    import os

    from coordinator_core.telemetry import op_latency

    os.environ.setdefault(op_latency.ORIGIN_ENV, op_latency.BENCHMARK)
