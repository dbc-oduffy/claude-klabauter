"""
coordinator_core.benchmarks.fact_layer_hot_path — OFFLINE renderer for the fact
layer's per-ceremony hot-path cost (`fl-core-04` C2,
docs/plans/2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.md).

Purpose: this module is the READER half of the fact-layer measurement — it
does not instrument anything (C1 owns the "fact_span" emission at the fact
boundary in `coordinator_core/session/session_facts.py`) and it does not arm
a ceiling (C4 owns `session_facts_budget.py`). It computes two independent
figures and renders them for C3 to state in the artifact:

  - the STRUCTURAL leg: deterministic git-spawn / file-read counts per fact,
    derived by enumerating the fact layer's own call sites in
    `coordinator_core/session/session_facts.py` and
    `coordinator_core/ops/ceremony/branch_resolution.py` — NOT measured
    against a live corpus. A spawn-and-read count needs no accumulation
    window; it is fixed by the code, so it is stated here as data rather than
    sampled.
  - the TIMING leg: process-time distributions read from the "fact_span" rows
    C1's instrumentation emits into `op-latency*.jsonl` (kind == "fact_span"),
    grouped by `invocation_id` when a row carries one, else by `sid`, into
    per-invocation aggregates, split into computed vs degraded populations (a
    degraded fact short-circuits and is systematically cheaper — blending
    the two would understate the computed cost and overstate the degraded
    one). `coordinator_core/quick_wrap_assemble/__init__.py::brief` mints and
    threads an `invocation_id` through all five facts it reads (see
    `record_fact_span`'s own docstring), so rows written by that ceremony
    group into a real per-ceremony aggregate; a row from any other caller
    still falls back to the `sid`-collapsed aggregate — see
    `state/bug-backlog/2026-08-27-fact-span-rows-cannot-yield-a-per-ceremo-d9be470c2039.yaml`.

This module is explicitly OFFLINE: it is not itself held to the brightline's
500ms per-process bar (it runs standalone, off the dispatch hot path), but its
own read cost is bounded rather than left open-ended (see `DEFAULT_TAIL_BYTES`
/ `DEFAULT_MAX_ROWS` below) and MUST be stated in whatever artifact reports its
output — a reader must never be left to assume this render is free.

Ambient context (secondary, load-dependent leg; DR-fact-layer-measurement-
method.md): this module also reads `ambient-load.jsonl` (written by
`coordinator_core.benchmarks.ambient_sampler`, NOT reused here beyond its
sink path — that module produces the corpus and exposes no join surface) and
joins each timing row's `t_start` to the nearest ambient sample by timestamp.
This join is CONTEXT ONLY (DR-344's 2026-08-21 amendment: process time and
spawn count are the axis; ambient load is never what a figure is adjudicated
or gated against) and is kept under its own field
(`RenderedReport.ambient_context`) so a downstream reader — C3, and anyone
reading C3's artifact — cannot accidentally fold it into an axis figure.

Neither `coordinator_core.benchmarks.ambient_sampler` nor
`coordinator_core.benchmarks.concurrency_probe` is otherwise reused: the
former only produces a corpus and exposes no join (this module writes its
own), the latter measures end-to-end latency of a REGISTERED, COMPUTE_ONLY op
by spawning `invoke` child processes — the six facts are deliberately not
registered ops (`session_facts.py`'s own docstring), so `concurrency_probe.py`
has no op name to dial and cannot measure this in-process facade at all.

Renders figures only — does not choose X (`session_facts_budget.py`, C4) and
does not gate anything.

Spec backlink: docs/plans/2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.md § C2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

FACT_SPAN_KIND = "fact_span"

DEFAULT_TAIL_BYTES = 8 * 1024 * 1024

DEFAULT_MAX_ROWS = 20_000

DEFAULT_AMBIENT_TAIL_BYTES = 2 * 1024 * 1024
DEFAULT_AMBIENT_MAX_ROWS = 20_000

FACT_NAMES = (
    "session_magnitude_attributed",
    "session_pickup_kind",
    "session_governing_plan",
    "session_diff_brightline",
    "session_terminal_sizings",
    "session_fold_sidecars",
)

FACT_WITH_NO_PRODUCTION_CONSUMER = "session_magnitude_attributed"

#: `_read_close_gate_facts`. NO SUCH FUNCTION EXISTS, in this repo or in git
PRODUCTION_CALL_SITE = "coordinator_core/quick_wrap_assemble/__init__.py::brief"

PRODUCTION_FACT_ROW_NAMES = frozenset(f"session_facts.{name}" for name in FACT_NAMES)


@dataclass(frozen=True)
class CallSite:

    kind: str
    call: str
    always: bool
    per_item: bool = False
    note: str = ""


STRUCTURAL_CALL_SITES: dict = {
    "session_magnitude_attributed": (
        CallSite(
            "git_spawn",
            "branch_resolution.py::_cached_session_commits (one git log "
            "--numstat --raw invocation, lru-cached per (worktree_root, sid) "
            "for this process's lifetime)",
            always=True,
        ),
    ),
    "session_pickup_kind": (
        CallSite(
            "file_read",
            "branch_resolution.py::_read_session_shape (session-shape.json)",
            always=True,
        ),
        CallSite(
            "file_read",
            "session_facts.py::_read_frontmatter_kind (the picked-up artifact's "
            "own frontmatter)",
            always=False,
            note="only when a pickup actually happened and the artifact path exists",
        ),
    ),
    "session_governing_plan": (
        CallSite(
            "file_read",
            "claimed_plan.py::list_held_plan_claims (session_id + claimed_at "
            "per held claim directory)",
            always=False,
            per_item=True,
            note="2 reads per held plan-claim directory; 0 when this session "
            "holds no claim",
        ),
        CallSite(
            "file_read",
            "session_facts.py::_read_frontmatter_status + "
            "_read_frontmatter_scope_mode (2 reads of the same resolved plan file)",
            always=False,
            note="only when a claim resolves to an existing plan file",
        ),
        CallSite(
            "git_spawn",
            "session_facts.py::_dirty_paths (git status --porcelain)",
            always=False,
            note="only when a claim resolves to an existing plan file (collision check)",
        ),
    ),
    "session_diff_brightline": (
        CallSite(
            "git_spawn",
            "branch_resolution.py::_cached_session_commits (shared cache: same "
            "call session_magnitude_attributed pays; a cache hit here costs "
            "zero additional spawns if that fact already ran this process)",
            always=True,
        ),
        CallSite(
            "git_spawn",
            "session_facts.py::_novel_loc_split (git log --numstat -M, not "
            "cache-shared with the call above)",
            always=True,
        ),
        CallSite(
            "git_spawn",
            "branch_resolution.py::_started_at_candidate_range (git log "
            "--since, inside analyze_session_scoping)",
            always=True,
        ),
        CallSite(
            "git_spawn",
            "branch_resolution.py::_trailer_reliable (git log -1 --format=%ct)",
            always=False,
            note="only when session_commit_count_attributed reports value==0 "
            "and started_at is present — the trailer-unreliable check",
        ),
        CallSite(
            "git_spawn",
            "session_attribution.py::detect_foreign_commits / "
            "range_is_contiguous_suffix (delegated; spawn count not "
            "decomposed here)",
            always=False,
            note="only reached when the trailer is proven unreliable — the "
            "uncommon branch; see analyze_session_scoping",
        ),
    ),
    "session_terminal_sizings": (
        CallSite(
            "git_spawn",
            "session_facts.py::_dirty_paths (git status --porcelain)",
            always=False,
            note="always when state/sizings/ exists as a directory; 0 when absent",
        ),
        CallSite(
            "file_read",
            "session_facts.py::_read_frontmatter_status (one read per "
            "*.yaml under state/sizings/)",
            always=False,
            per_item=True,
            note="one read per candidate sizing record; 0 when the directory "
            "is absent or empty",
        ),
    ),
    "session_fold_sidecars": (
        CallSite(
            "git_spawn",
            "session_facts.py::_dirty_paths (git status --porcelain)",
            always=False,
            note="only when the directory scan found at least one sidecar",
        ),
    ),
}


@dataclass(frozen=True)
class StructuralCounts:

    fact: str
    git_spawns_min: int
    git_spawns_max: int
    file_reads_min: int
    file_reads_max: int
    per_item_notes: tuple
    call_sites: tuple


def structural_counts_for(fact_name: str) -> StructuralCounts:
    """Deterministic structural counts for `fact_name`, from `STRUCTURAL_CALL_SITES`.

    Raises `KeyError` for a name outside `FACT_NAMES` — there is no meaningful
    degrade-open here: an unrecognised fact name is a caller bug, not data.
    """
    sites = STRUCTURAL_CALL_SITES[fact_name]
    spawns_min = spawns_max = reads_min = reads_max = 0
    per_item_notes = []
    for site in sites:
        if site.per_item:
            per_item_notes.append(f"{site.kind}: {site.call} ({site.note})")
            continue
        if site.kind == "git_spawn":
            spawns_max += 1
            if site.always:
                spawns_min += 1
        elif site.kind == "file_read":
            reads_max += 1
            if site.always:
                reads_min += 1
    return StructuralCounts(
        fact=fact_name,
        git_spawns_min=spawns_min,
        git_spawns_max=spawns_max,
        file_reads_min=reads_min,
        file_reads_max=reads_max,
        per_item_notes=tuple(per_item_notes),
        call_sites=tuple(sites),
    )


def all_structural_counts() -> dict:
    """`structural_counts_for` for every name in `FACT_NAMES`, in that order."""
    return {name: structural_counts_for(name) for name in FACT_NAMES}


def read_fact_span_rows(
    repo_root: Path,
    *,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list:
    from coordinator_core.telemetry.op_latency import sink_generations, tail_entries

    rows: list = []
    for path in sink_generations(repo_root):
        entries, _head_truncated = tail_entries(path, tail_bytes=tail_bytes, max_rows=max_rows)
        rows.extend(entry for entry in entries if _is_production_fact_span(entry))
    return rows


def _is_production_fact_span(entry) -> bool:
    """A `"fact_span"` row carrying a real production measurement.

    Excludes synthetic rows sharing the `session_facts.` prefix — see
    `PRODUCTION_FACT_ROW_NAMES`. A BUFFERED row (no `fact` key, a `facts` map
    instead) has no single name to allow-list and is admitted on shape; the
    per-fact names inside it are filtered by `compute_timing_distributions`
    against `FACT_NAMES`.
    """
    if not isinstance(entry, dict) or entry.get("kind") != FACT_SPAN_KIND:
        return False
    if isinstance(entry.get("facts"), dict):
        return True
    return entry.get("fact") in PRODUCTION_FACT_ROW_NAMES


@dataclass
class FactTimingStats:

    fact: str
    computed_ms: list = field(default_factory=list)
    degraded_ms: list = field(default_factory=list)

    @property
    def computed_count(self) -> int:
        return len(self.computed_ms)

    @property
    def degraded_count(self) -> int:
        return len(self.degraded_ms)

    @property
    def degraded_total_ms(self) -> float:
        return sum(self.degraded_ms)

    def percentile(self, fraction: float) -> Optional[float]:
        if not self.computed_ms:
            return None
        ordered = sorted(self.computed_ms)
        if len(ordered) == 1:
            return ordered[0]
        idx = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return ordered[idx]

    def as_dict(self) -> dict:
        return {
            "fact": self.fact,
            "computed_count": self.computed_count,
            "degraded_count": self.degraded_count,
            "degraded_total_ms": round(self.degraded_total_ms, 3),
            "p50_ms": self.percentile(0.50),
            "p95_ms": self.percentile(0.95),
            "max_ms": max(self.computed_ms) if self.computed_ms else None,
            "total_ms": round(sum(self.computed_ms), 3),
        }


def compute_timing_distributions(rows) -> dict:
    """Per-fact `FactTimingStats` plus an aggregate, from parsed "fact_span" rows.

    TWO row shapes are accepted, because C1 and C2 were authored against
    different ones and only the first is actually on disk:

    - PER-FACT (what `session_facts._timed_fact` emits, and the only shape the
      live sink contains): `{"kind": "fact_span", "t_start": float,
      "sid": str|None, "invocation_id": str|None,
      "fact": "session_facts.<name>", "elapsed_ms": float,
      "process_ms": float|None, "outcome": "computed"|"degraded"}`. Rows are
      grouped by `invocation_id` when present, else `sid`, to recover the
      per-ceremony aggregate — the read-time regrouping `record_fact_span`'s
      docstring names as the cost of not buffering.
      `quick_wrap_assemble.brief` now passes `invocation_id`, so rows it
      writes group into a real per-ceremony aggregate; a row from a caller
      that still omits it degrades to the `sid`-collapsed aggregate.
    - BUFFERED (this plan's PREFERRED shape, never built):
      `{..., "facts": {<name>: {"elapsed_ms": float, "degraded": bool}}}`.
      Kept because it is the shape this module's own tests were written
      against; a reader that dropped it would silently zero any corpus written
      if the buffered flush hook is ever added.

    Reading only the buffered shape is why the first artifact reported the
    whole timing leg "unobserved" over a corpus that already held rows.

    A malformed row (not a dict, carrying neither shape) is skipped rather
    than raising — this is a reader over a sink several live processes append
    to.

    Returns `{"per_fact": {<fact_name>: FactTimingStats, ...}, "aggregate":
    FactTimingStats}` — `"aggregate"` sums every fact's own elapsed_ms per
    ceremony invocation into one row-per-invocation total, the number AC4
    requires reported ALONGSIDE (never instead of) the per-fact figures.

    A per-fact row with no `sid` cannot be attributed to a ceremony
    invocation, so it contributes to its own fact's distribution but NOT to
    the aggregate — counting it as its own one-fact "invocation" would report
    an aggregate far cheaper than any real ceremony.
    """
    per_fact: dict = {name: FactTimingStats(fact=name) for name in FACT_NAMES}
    aggregate = FactTimingStats(fact="__aggregate__")

    by_invocation: dict = {}

    def _short(fact_name: str) -> str:
        return fact_name.split(".")[-1]

    for row in rows:
        if not isinstance(row, dict):
            continue

        facts = row.get("facts")
        if isinstance(facts, dict):
            invocation_total = 0.0
            invocation_had_computed = False
            for fact_name, breakdown in facts.items():
                if not isinstance(breakdown, dict):
                    continue
                elapsed = breakdown.get("elapsed_ms")
                if not isinstance(elapsed, (int, float)):
                    continue
                stats = per_fact.setdefault(fact_name, FactTimingStats(fact=fact_name))
                if breakdown.get("degraded"):
                    stats.degraded_ms.append(float(elapsed))
                else:
                    stats.computed_ms.append(float(elapsed))
                    invocation_total += float(elapsed)
                    invocation_had_computed = True
            if invocation_had_computed:
                aggregate.computed_ms.append(invocation_total)
            continue

        raw_name = row.get("fact")
        elapsed = row.get("elapsed_ms")
        if not isinstance(raw_name, str) or not isinstance(elapsed, (int, float)):
            continue

        fact_name = _short(raw_name)
        stats = per_fact.setdefault(fact_name, FactTimingStats(fact=fact_name))
        if row.get("outcome") == "degraded":
            stats.degraded_ms.append(float(elapsed))
            continue

        stats.computed_ms.append(float(elapsed))
        invocation_id = row.get("invocation_id")
        grouping_key = invocation_id if isinstance(invocation_id, str) else row.get("sid")
        if isinstance(grouping_key, str):
            by_invocation.setdefault(grouping_key, {})[fact_name] = float(elapsed)

    for breakdown in by_invocation.values():
        aggregate.computed_ms.append(sum(breakdown.values()))

    return {"per_fact": per_fact, "aggregate": aggregate}


def read_ambient_samples(
    repo_root: Path,
    *,
    tail_bytes: int = DEFAULT_AMBIENT_TAIL_BYTES,
    max_rows: int = DEFAULT_AMBIENT_MAX_ROWS,
) -> list:
    from coordinator_core.benchmarks.ambient_sampler import _sink_path
    from coordinator_core.lifecycle import git_common_dir
    from coordinator_core.telemetry.op_latency import tail_entries

    try:
        common_dir = git_common_dir(repo_root)
    except (RuntimeError, OSError):
        return []
    sink = _sink_path(common_dir)
    entries, _head_truncated = tail_entries(sink, tail_bytes=tail_bytes, max_rows=max_rows)
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("t"), (int, float))]


def nearest_ambient_sample(t_start: float, samples: list) -> Optional[dict]:
    if not samples:
        return None
    return min(samples, key=lambda s: abs(s["t"] - t_start))


def join_ambient_context(fact_span_rows: list, ambient_samples: list) -> list:
    joined = []
    for row in fact_span_rows:
        if not isinstance(row, dict):
            continue
        t_start = row.get("t_start")
        if not isinstance(t_start, (int, float)):
            continue
        joined.append(
            {
                "t_start": t_start,
                "sid": row.get("sid"),
                "ambient": nearest_ambient_sample(float(t_start), ambient_samples),
            }
        )
    return joined


@dataclass
class RenderedReport:

    structural: dict
    timing: dict
    ambient_context: list
    fact_with_no_production_consumer: str = FACT_WITH_NO_PRODUCTION_CONSUMER
    production_call_site: str = PRODUCTION_CALL_SITE

    def as_dict(self) -> dict:
        return {
            "structural": {
                name: {
                    "git_spawns_min": counts.git_spawns_min,
                    "git_spawns_max": counts.git_spawns_max,
                    "file_reads_min": counts.file_reads_min,
                    "file_reads_max": counts.file_reads_max,
                    "per_item_notes": list(counts.per_item_notes),
                }
                for name, counts in self.structural.items()
            },
            "timing": {
                "per_fact": {
                    name: stats.as_dict() for name, stats in self.timing["per_fact"].items()
                },
                "aggregate": self.timing["aggregate"].as_dict(),
            },
            "ambient_context": self.ambient_context,
            "fact_with_no_production_consumer": self.fact_with_no_production_consumer,
            "production_call_site": self.production_call_site,
        }


def render(
    repo_root: Path,
    *,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    ambient_tail_bytes: int = DEFAULT_AMBIENT_TAIL_BYTES,
    ambient_max_rows: int = DEFAULT_AMBIENT_MAX_ROWS,
    include_ambient: bool = True,
) -> RenderedReport:
    structural = all_structural_counts()
    fact_span_rows = read_fact_span_rows(repo_root, tail_bytes=tail_bytes, max_rows=max_rows)
    timing = compute_timing_distributions(fact_span_rows)

    ambient_context: list = []
    if include_ambient:
        ambient_samples = read_ambient_samples(
            repo_root, tail_bytes=ambient_tail_bytes, max_rows=ambient_max_rows
        )
        ambient_context = join_ambient_context(fact_span_rows, ambient_samples)

    return RenderedReport(
        structural=structural,
        timing=timing,
        ambient_context=ambient_context,
    )
