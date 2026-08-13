"""
coordinator_core.benchmarks -- Per-op end-to-end latency benchmark harness.

Purpose: measures caller-experienced spawn-to-exit latency of the
`invoke` CLI entrypoint (see coordinator_core.invoke.__main__ for the exact
command form) under DR-215's command-type spawn-per-call execution model,
gates measured distributions against a two-level (OpClass-tier default +
per-op override) budget manifest, and persists an append-only
code_sha-keyed baseline store.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10
"""

from __future__ import annotations
