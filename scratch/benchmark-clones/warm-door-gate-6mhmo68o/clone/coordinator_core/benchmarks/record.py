"""Conformance-record data model — the qsub-01<->qsub-03 CONTRACT surface.

This module defines the schema-shape seam between the latency benchmark harness
(qsub-01, this plan) and the future DoD merge-gate / CI enforcement consumer
(qsub-03). Any change to `ConformanceRecord`'s field set or JSON shape is a
contract change across that seam — coordinate with qsub-03 before altering it.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C1
(AC2 field enumeration).

Floor-scope semantics (load-bearing for qsub-03's interpretation of this record):
the cold-start floor (`cold_start_floor_ms`, `floor_cov`) is measured ONCE per
harness run (see floor.py / harness.py), not once per op. `floor_scope="run"`
(the default, and the only value wave-1 produces) means every ConformanceRecord
emitted in the same run shares that single floor draw — `run_id` identifies
which draw a record belongs to. A consumer that compares `floor_delta_ms`
across two different ops within the same run is NOT looking at two independent
paired measurements; it is looking at two different op-latencies each offset
by the SAME shared floor. Do not over-interpret cross-op `floor_delta_ms`
deltas as if the floor were re-measured per op. A hypothetical future
`floor_scope="per_op"` would change that invariant; wave-1 never emits it.

Pure data model: no I/O, no timing, no subprocess. See timer.py/harness.py for
the measurement side; gate.py for verdict computation.
"""

from __future__ import annotations

import json
import platform
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 2
"""Pinned schema version stamped onto every emitted record. Bump only in
lockstep with a coordinated qsub-03 contract change.

v1 -> v2 (this bump): added `machine` (machine identity, Optional[str]) and
the ambient-context trio `ambient_before`/`ambient_after`/`ambient_delta`
(Optional[dict]). `from_json` accepts a v1 payload (none of these keys
present) by defaulting every one of them to None -- see from_json()."""

# Review: code-reviewer (Slice B F5, nit) — named constants for floor_scope's
# documented "run"|"per_op" enum, mirroring gate.py's VERDICT_* constant pattern,
# so callers (harness.py) reference a name instead of a bare string literal.
FLOOR_SCOPE_RUN = "run"
FLOOR_SCOPE_PER_OP = "per_op"


@dataclass(frozen=True)
class Tolerance:
    """Gate tolerance band descriptor.

    kind: "relative" (band = target_ms * (1 + value)) or "absolute"
    (band = target_ms + value). Interpreted by gate.py — this dataclass only
    carries the value, it does not compute the band.
    """

    kind: str
    value: float

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Tolerance":
        return Tolerance(kind=data["kind"], value=data["value"])


@dataclass(frozen=True)
class ConformanceRecord:
    """One op's SLA-conformance measurement for one benchmark run.

    Carries every AC2-pinned field. Round-trippable via to_json()/from_json().
    """

    op: str
    """Op name, e.g. 'ping' or 'coverage.gate' — matches OP_CLASSIFICATION keys."""

    op_class: str
    """OpClass tier name (e.g. 'COMPUTE_ONLY'), stringified — see
    coordinator_core.authz.classification.OpClass."""

    target_ms: float
    """Resolved end-to-end budget target in milliseconds (budget.py output)."""

    tolerance: Tolerance
    """Tolerance band descriptor ({kind, value}) applied to target_ms by gate.py."""

    gating_statistic: str
    """Name of the statistic the verdict was computed against — pinned to
    'min' per this plan's resolved gate design (min vs. raw end-to-end target,
    no floor subtraction)."""

    gating_statistic_value: float
    """The actual numeric value of gating_statistic for this op/run."""

    min: float
    p50: float
    p95: float
    p99: float
    """Percentile/extremum summary of the raw end-to-end sample distribution,
    all in milliseconds, spawn-to-exit inclusive."""

    sample_count: int
    """N — number of valid (exit-0, non-error) timed samples collected."""

    cold_start_floor_ms: float
    """The run-level `ping`-derived cold-start floor statistic (see floor.py).
    This is a measured baseline, not a target — it characterizes the DR-215
    spawn-per-call floor shared by every record in this run (floor_scope)."""

    floor_delta_ms: Optional[float]
    """Signed, nullable: op's end-to-end statistic minus cold_start_floor_ms.
    Advisory-only — NEVER a gating quantity (subtracting two independently-
    noisy ~59ms cold-start draws amplifies variance; sign flips on sub-2ms
    contributions are expected, not data errors). Distinct from
    cold_start_floor_ms: the floor is the baseline draw, this is the
    (unreliable) op-relative offset computed from it."""

    floor_cov: float
    """Coefficient of variation of the floor-stability pass (floor.py),
    characterizing how noisy the shared cold-start floor draw was for this run."""

    floor_scope: str = "run"
    """Enum "run"|"per_op" — declares whether cold_start_floor_ms/floor_cov
    were measured once per run (default, wave-1's only emitted value) or once
    per op. Distinct from run_id: floor_scope names the MEASUREMENT GRANULARITY
    convention; run_id names WHICH run's shared draw a given record used."""

    run_id: str = ""
    """Identifier shared by every ConformanceRecord emitted from the same
    harness run/invocation of measure_floor — lets a consumer group records
    that share one floor draw. Distinct from baseline_id: run_id scopes a
    single benchmark execution; baseline_id scopes a persisted-store entry
    (see baseline_store.py), which may outlive any one run."""

    verdict: str = "advisory"
    """gate.py's pure-function output: 'pass' | 'fail' | 'advisory'."""

    baseline_id: str = ""
    """Identifier of the baseline-store entry this record was persisted as
    (see baseline_store.py, C7) — append-only and code_sha-keyed. Distinct
    from code_sha: baseline_id is the store's own record key (may embed
    code_sha plus op plus a sequence/timestamp disambiguator); code_sha alone
    is just the git commit under test."""

    code_sha: str = ""
    """Full git commit SHA the harness measured against, captured ONCE at
    harness.run entry (not re-shelled per op) — deterministic run identity
    even if HEAD moves under a concurrent session mid-sweep."""

    timestamp: str = ""
    """ISO-8601 timestamp of when this record was produced."""

    runner_isolation_mode: str = "shared"
    """Describes the execution environment isolation for this run (e.g.
    'shared' = ran on a shared/live working tree, as opposed to an isolated
    CI runner). Default 'shared' reflects wave-1's ad-hoc dev-loop harness."""

    machine: Optional[str] = None
    """Machine identity this record was measured on, composed via
    compose_machine_id() (platform.system() + platform.node(), lowercased,
    joined). Optional rather than bare `str` so from_json() can construct a
    migrated v1 record (no `machine` key present) without raising -- v1
    records carry no machine identity and are None here, never a comparable
    baseline (see baseline_store's machine partition, C3). Timings are
    per-machine, full stop: a None-machine record names no box its timings
    are valid under."""

    ambient_before: Optional[dict] = None
    """Ambient-load snapshot (coordinator_core.benchmarks.ambient_sampler.
    take_sample() shape: {t, live_sessions, claude_procs, cpu_pct,
    ram_free_mb, ram_total_mb}) taken in-process at run start. Every field
    inside degrades to null independently on sampler failure -- never
    raises. Distinct from a post-hoc join against ambient_sampler's
    standalone ambient-load.jsonl daemon (which nothing guarantees is
    running, and which can be stale by up to DEFAULT_INTERVAL_SECONDS):
    this is measured for THIS run, at write time."""

    ambient_after: Optional[dict] = None
    """Ambient-load snapshot (same shape as ambient_before) taken in-process
    at run end, outside the timed measurement window."""

    ambient_delta: Optional[dict] = None
    """Per-field (ambient_after - ambient_before) delta for every numeric
    field ambient_before/ambient_after share (live_sessions, claude_procs,
    cpu_pct, ram_free_mb, ram_total_mb) -- see compute_ambient_delta(). A
    field degrades to null in the delta if either side is null or either
    snapshot itself is None; never raises."""

    schema_version: int = SCHEMA_VERSION
    """Pinned contract version for this record shape. See module-level
    SCHEMA_VERSION."""

    def to_json(self) -> str:
        """Serialize this record to a JSON string. Round-trip pair: from_json()."""
        data = asdict(self)
        # Review: code-reviewer (Slice A F5, nit) — asdict() already recurses
        # into Tolerance (a nested dataclass) and produces an equivalent dict;
        # this explicit re-assignment is belt-and-braces future-proofing in
        # case Tolerance ever grows a non-trivially-serializable field, kept
        # intentionally rather than dropped.
        data["tolerance"] = self.tolerance.to_dict()
        return json.dumps(data, sort_keys=True)

    @staticmethod
    def from_json(payload: str) -> "ConformanceRecord":
        """Deserialize a JSON string produced by to_json() back into a
        ConformanceRecord. Round-trip pair: to_json().

        Accepts a v1 payload (no `machine`/`ambient_before`/`ambient_after`/
        `ambient_delta` keys) without raising: every one of those fields
        defaults to None on the dataclass, so simply omitting an absent key
        from the ConformanceRecord(**data) call below reconstructs a v1
        record with those fields set to None -- the real migration path a
        future schema bump reuses."""
        data = json.loads(payload)
        data = dict(data)
        data["tolerance"] = Tolerance.from_dict(data["tolerance"])
        return ConformanceRecord(**data)


def compose_machine_id() -> str:
    """Compose this box's machine identity: platform.system() and
    platform.node(), lowercased and joined, stable across runs on one box
    and distinct across boxes. Used to populate ConformanceRecord.machine
    at measurement time."""
    return f"{platform.system()}-{platform.node()}".lower()


def compute_ambient_delta(before: Optional[dict], after: Optional[dict]) -> Optional[dict]:
    """Per-field (after - before) delta over the numeric fields
    ambient_sampler.take_sample() produces (live_sessions, claude_procs,
    cpu_pct, ram_free_mb, ram_total_mb). Never raises: returns None if
    either snapshot itself is None, and degrades an individual field to
    null in the result if either side of that field is null -- same
    degrade-to-null discipline take_sample() itself follows."""
    if before is None or after is None:
        return None
    delta: dict[str, Optional[float]] = {}
    for key in ("live_sessions", "claude_procs", "cpu_pct", "ram_free_mb", "ram_total_mb"):
        before_value = before.get(key)
        after_value = after.get(key)
        if before_value is None or after_value is None:
            delta[key] = None
        else:
            delta[key] = after_value - before_value
    return delta
