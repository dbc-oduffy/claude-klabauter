"""
coordinator_core.telemetry — durable per-op latency + ambient-load measurement.

Purpose: measurement-only instrumentation added to establish whether the engine
meets the ratified load norm (docs/wiki/machine-load-norm.md: 50-70 active LLMs
as the AVERAGE, not peak). This package records facts; it does not remediate
anything. See coordinator_core.telemetry.op_latency and
coordinator_core.benchmarks.ambient_sampler.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations
