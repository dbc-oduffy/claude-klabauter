
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


def fixture_settings_home(parent):
    """A context pinning ``COORDINATOR_SETTINGS_HOME`` to ``<parent>/settings-home``; restored on exit.

    Trap: run tokens and the live-run index resolve under the settings home, never the
    fixture repo, so a script-mode benchmark that mints without this writes into the
    operator's REAL machine-local store, which every live engine scan reads (2026-11-04:
    56 leaked fixture homes, 28k tokens, hook round-trips 3.3s). pytest runs are already
    quarantined by ``conftest._quarantine_real_home``; a script run has no such harness.

    ``parent`` must outlive every process that reads the fixture (a ``--build-only`` then
    ``--prebuilt`` pair resolves the same ``parent``), and must not be inside a fixture repo.
    Gate: ``tests/test_benchmark_mints_scope_settings_home.py``.
    """
    import os
    from pathlib import Path
    from unittest import mock

    return mock.patch.dict(os.environ, {"COORDINATOR_SETTINGS_HOME": str(Path(parent).resolve() / "settings-home")})
