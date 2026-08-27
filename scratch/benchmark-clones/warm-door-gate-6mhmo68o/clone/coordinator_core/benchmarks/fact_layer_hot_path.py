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
    grouped by `sid` into per-invocation aggregates, split into computed vs
    degraded populations (a degraded fact short-circuits and is systematically
    cheaper — blending the two would understate the computed cost and
    overstate the degraded one).

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

#: The new row kind C1 emits (module docstring, `coordinator_core.telemetry.op_latency`
#: Anti-scope: "no second FILE" — this is a new `kind` value in the existing sink,
#: not a parallel telemetry file). Neither `pairing_summary` nor `breach_summary`
#: nor `cost_census` recognises this kind; all three already skip any row whose
#: `kind` matches neither "started" nor "complete" ("composition" is the sibling
#: precedent this mirrors).
FACT_SPAN_KIND = "fact_span"

#: Bound on the op-latency corpus read (task body: "a full-corpus JSON-parse pass
#: is seconds of process time on a box carrying ~50 peers, and the module already
#: ships the bounded primitive for this" — `op_latency.py :: tail_entries`). 8MB is
#: comfortably above one rotated generation's typical "fact_span" row density
#: (each row is a small per-ceremony breakdown map, not a per-fact row — see C1's
#: buffered-emission design) while staying a small fraction of the ~140MB/five-
#: generation corpus this bound exists to avoid parsing in full.
DEFAULT_TAIL_BYTES = 8 * 1024 * 1024

#: Row cap paired with the byte bound above — belt-and-suspenders against a
#: pathologically dense generation; `tail_entries` enforces both independently.
DEFAULT_MAX_ROWS = 20_000

#: Same discipline for the ambient-load sink: it is far smaller per-sample (six
#: scalar fields, one line per `ambient_sampler` tick, default 30s interval) but
#: is read with the same bounded primitive rather than assumed free.
DEFAULT_AMBIENT_TAIL_BYTES = 2 * 1024 * 1024
DEFAULT_AMBIENT_MAX_ROWS = 20_000

#: The six facts `session_facts.py` serves (module docstring: "SERVES ALL FIVE
#: `fl-core-02` FACTS ... AND the fold-execution-record sidecar scan"). Stated
#: once here as the canonical enumeration order every per-fact figure below
#: reports against, so a consumer never has to reconstruct the set from the
#: timing corpus (which may not have observed every fact yet).
FACT_NAMES = (
    "session_magnitude_attributed",
    "session_pickup_kind",
    "session_governing_plan",
    "session_diff_brightline",
    "session_terminal_sizings",
    "session_fold_sidecars",
)

#: `session_magnitude_attributed` has no production consumer today (AC6's
#: substrate finding 1 — plan Problem section, "Two substrate facts... verified
#: this session"). Named here so a reader of the structural/timing tables below
#: sees the same finding this module's caller (C3) is required to restate, not
#: a number floating with no context for why its production-row population is
#: expected to read zero.
FACT_WITH_NO_PRODUCTION_CONSUMER = "session_magnitude_attributed"

#: The facade's one production call site (Problem section, substrate finding 0):
#: `quick_wrap_assemble/__init__.py :: _read_close_gate_facts` calls five of the
#: six facts in sequence. Stated here so a consumer of this module's figures does
#: not have to re-derive "per-ceremony" means "per this one call site" from the
#: timing corpus alone.
PRODUCTION_CALL_SITE = "coordinator_core/quick_wrap_assemble/__init__.py::_read_close_gate_facts"


# ---------------------------------------------------------------------------
# Structural leg — deterministic call-site counts, not a live corpus.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSite:
    """One deterministic git-spawn or file-read call site for a served fact.

    `always` distinguishes an unconditional call (every invocation of the
    fact pays it) from a conditional one (`always=False`) reached only on
    some data-dependent branch — e.g. `session_pickup_kind` reads
    `session-shape.json` on every call, but only reads a picked-up artifact's
    frontmatter `kind:` when a pickup actually happened. A conditional site's
    `per_item` flag (when True) means the count scales with a runtime-sized
    collection (e.g. one file read per held plan claim, per terminal sizing
    record) rather than being a fixed 0-or-1 — `structural_counts_for` reports
    such a site's contribution as `None` (unbounded without a live corpus) and
    names it in `notes` rather than fabricating a number.
    """

    kind: str  # "git_spawn" | "file_read"
    call: str  # grep-able call-site name
    always: bool
    per_item: bool = False
    note: str = ""


#: Enumerated by reading `coordinator_core/session/session_facts.py` and
#: `coordinator_core/ops/ceremony/branch_resolution.py` (2026-08-27, this
#: chunk) — the deterministic count source the task body asks for, NOT a live
#: corpus. Each entry cites the call site it counts so a future reader can
#: re-verify the enumeration against the code rather than trusting this table
#: blind. `git_common_dir` (an lru-cached, pure-Python upward directory walk —
#: see its own docstring's "hot path may treat this as zero-spawn") is
#: deliberately excluded from every `git_spawn` count below: it never shells
#: out to git.
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
    """Structural (git-spawn, file-read) counts for one fact.

    `git_spawns_min`/`git_spawns_max` and `file_reads_min`/`file_reads_max`
    bound the fixed-count call sites (`CallSite.per_item is False`) —
    `_min` counts only `always=True` sites, `_max` adds every conditional
    site once. Per-item sites (a count that scales with a runtime-sized
    collection — held plan claims, terminal sizing records) are NOT folded
    into either bound; they are reported separately in `per_item_notes` so a
    reader is never handed a max that quietly assumes some fixed collection
    size.
    """

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


# ---------------------------------------------------------------------------
# Timing leg — read the "fact_span" rows C1 emits.
# ---------------------------------------------------------------------------


def read_fact_span_rows(
    repo_root: Path,
    *,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list:
    """Bounded read of every `kind == "fact_span"` row across the live
    op-latency sink plus its rotated generations.

    Routes through `coordinator_core.telemetry.op_latency.sink_generations`
    (newest-first) and `tail_entries` (bounded by `tail_bytes`/`max_rows` PER
    generation) — never a full-corpus parse (task body: "a full-corpus
    JSON-parse pass is seconds of process time on a box carrying ~50 peers,
    and the module already ships the bounded primitive for this"). Returns
    `[]` rather than raising when the sink cannot be resolved or is empty —
    this is an offline reader over a corpus C1's instrumentation may not have
    written anything into yet.
    """
    from coordinator_core.telemetry.op_latency import sink_generations, tail_entries

    rows: list = []
    for path in sink_generations(repo_root):
        entries, _head_truncated = tail_entries(path, tail_bytes=tail_bytes, max_rows=max_rows)
        rows.extend(entry for entry in entries if entry.get("kind") == FACT_SPAN_KIND)
    return rows


@dataclass
class FactTimingStats:
    """Process-time distribution for one fact, computed vs degraded split.

    `computed_ms`/`degraded_ms` are the raw elapsed-ms samples for each
    population — kept separate per C1's own body ("a degraded fact
    short-circuits and is systematically cheaper"). `p50`/`p95`/`max` below
    are computed over `computed_ms` only, matching the population the fact
    layer's steady-state cost describes; a degraded sample's own count and
    total are still reported (`degraded_count`, `degraded_total_ms`) so a
    reader can see the degraded population exists without it skewing the
    computed distribution.
    """

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
        """Index-based percentile over sorted `computed_ms`, no interpolation
        (same rule as `op_latency.py::_percentile_idx`, restated here rather
        than imported — that helper is private to its own module)."""
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

    Expected row shape (C1's buffered-emission design, one row per ceremony
    invocation): `{"kind": "fact_span", "t_start": float, "sid": str|None,
    "facts": {<fact_name>: {"elapsed_ms": float, "degraded": bool}, ...}}`.
    A malformed row (not a dict, no "facts" mapping) is skipped rather than
    raising — this is a reader over a sink several live processes append to.

    Returns `{"per_fact": {<fact_name>: FactTimingStats, ...}, "aggregate":
    FactTimingStats}` — `"aggregate"` sums every fact's own elapsed_ms per
    ceremony invocation into one row-per-invocation total, the number AC4
    requires reported ALONGSIDE (never instead of) the per-fact figures.
    """
    per_fact: dict = {name: FactTimingStats(fact=name) for name in FACT_NAMES}
    aggregate = FactTimingStats(fact="__aggregate__")

    for row in rows:
        if not isinstance(row, dict):
            continue
        facts = row.get("facts")
        if not isinstance(facts, dict):
            continue

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

    return {"per_fact": per_fact, "aggregate": aggregate}


# ---------------------------------------------------------------------------
# Ambient context join — CONTEXT ONLY, never an adjudication axis.
# ---------------------------------------------------------------------------


def read_ambient_samples(
    repo_root: Path,
    *,
    tail_bytes: int = DEFAULT_AMBIENT_TAIL_BYTES,
    max_rows: int = DEFAULT_AMBIENT_MAX_ROWS,
) -> list:
    """Bounded read of `ambient-load.jsonl` (written by
    `coordinator_core.benchmarks.ambient_sampler`).

    Same bounded-read discipline as `read_fact_span_rows` — `tail_entries`,
    never a full parse. `ambient_sampler.py` does not rotate its sink (no
    generation walk needed), so this reads exactly one file. Returns `[]`
    when the sink is absent or empty — per DR-fact-layer-measurement-method.md,
    the sampler may not have accumulated a sink yet when this runs, and that
    is an observed fact to report, not a reason to block.
    """
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
    """The ambient sample whose `"t"` is closest to `t_start`, or `None` if
    `samples` is empty.

    Linear scan — `samples` is bounded by `read_ambient_samples`'s own
    `max_rows`, so this is never asked to scan an unbounded list.
    """
    if not samples:
        return None
    return min(samples, key=lambda s: abs(s["t"] - t_start))


def join_ambient_context(fact_span_rows: list, ambient_samples: list) -> list:
    """Nearest-timestamp join of each fact_span row's `t_start` to an ambient
    sample — CONTEXT ONLY (see module docstring). Returns a list of
    `{"t_start": float, "sid": str|None, "ambient": dict|None}`, one entry
    per row carrying a numeric `t_start`; a row with no ambient corpus to
    join against (or a malformed `t_start`) still appears, with `"ambient":
    None`, rather than being silently dropped.
    """
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


# ---------------------------------------------------------------------------
# Top-level render — assembles the structural + timing (+ ambient context)
# figures for C3 to state in the artifact. Renders; does not gate.
# ---------------------------------------------------------------------------


@dataclass
class RenderedReport:
    """The complete rendered figure set this module produces for C3.

    `structural` and `timing` are the two load-independent legs DR-344's
    amendment requires as the axis. `ambient_context` is the secondary,
    load-dependent leg DR-fact-layer-measurement-method.md rules taken as
    context only — kept in its own field so nothing here reads it as an
    adjudication input.
    """

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
    """Assemble the full rendered report: structural counts (pure, no I/O),
    the timing distributions read from `op-latency*.jsonl`, and (unless
    `include_ambient=False`) the ambient-context join.

    This is the OFFLINE entry point — it does bounded but real file I/O
    (`read_fact_span_rows`, `read_ambient_samples`) and its own cost must be
    stated by the caller (C3) rather than assumed free, per this module's
    own docstring.
    """
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
