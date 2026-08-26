"""
coordinator_core.op_census.meter — per-op process time and spawn count, with the
population it was computed over stated on the answer.

Purpose: the brightline is stated in process time and spawn count (CLAUDE.md
§ The brightline), and no such figure existed for a single op killed in the
2026-08-23 sweep — every K-045..K-055 entry reads "wall clock; no process-time
figure on record", because the instrument that would have produced them,
`op_census.report`, was killed by the bar it reports on. This module is the
rebuild. It is deliberately NOT a port: see § What the predecessor got wrong.

The requirement, stated once: **any session can ask what an op costs in process
time and spawns, and get an answer that is not contaminated, not truncated, and
not wall clock.**

WHAT THE PREDECESSOR GOT WRONG (all three are ACs here, not aspirations)

  1. Silent truncation. `op_census.report`'s surviving sibling reads a 6MB tail
     and discloses the loss in `source.head_truncated` — a field four sessions
     read past, filing counts 8x to 20x low
     (`state/bug-backlog/2026-08-23-op-census-breaches-undercounts-and-selects-on-outliers.yaml`
     D1). This module has no truncating read. A population it cannot read
     completely raises `PopulationIncomplete`; it never returns a short answer
     wearing a flag.
  2. Contaminated population. Test and benchmark dispatches were recorded
     indistinguishably from real ones. `ping` alone carried 10,832 completions
     in seven days, none of them production. Fixed at the SINK
     (`telemetry.op_latency.invocation_origin`), because measurement showed
     there was no read-time filter to build: `route` and `source_path` describe
     TRANSPORT, and origin is a property of the caller. Rows written before that
     field existed carry origin `unknown` here and are NEVER folded into
     production — an unknown-origin count is not a production count.
  3. Unfiltered-versus-filtered ambiguity. A prior scan silently discarded 75.1%
     of the corpus behind a `route`-present filter. Every result this module
     returns carries a `Population` describing exactly what it read.

WHY WINDOWED IS THE DEFAULT AND NOT A CONCESSION

Measured on the live sink, 2026-08-25, `time.process_time()` (never wall clock),
recorded at `state/audits/2026-08-25-the-meter-corpus-shape-spike.md`:

    full corpus, 4 generations, 456,194 rows, 113.5MB ... 875ms, zero spawns
    windowed, current generation, 29,644 rows ..........  62ms
    bounded 6MB tail (the incumbent, rejected) ......... 47ms, TRUNCATED

So the corpus was never the cost. The predecessor died at 11,735ms — thirteen
times the price of reading literally everything. A full scan misses the 500ms
bar by 1.75x, not by the order of magnitude the tension was framed as. That
retires the assumed trade between completeness and the bar:

  - Windowed is the DEFAULT because it is far inside the bar and answers the
    question a session actually asks, not because completeness is unaffordable.
    Cold, whole-tree, spawning nothing: 140.6ms (verified through
    `benchmarks.process_time.single_invocation_tree_process_time`, procs=1). The
    62ms above is the parse alone and 93.8ms is this module's self-report, which
    starts inside `_main` and so excludes interpreter start — quote 140.6ms.
  - Full-corpus is a NAMED, non-default mode that reports its own cost on every
    answer (`meter_process_ms`). Measured through THIS module rather than as a
    bare parse, all-time is ~1,156ms over 235,721 counted rows — the 875ms above
    is the parse floor, and the difference is this module's own aggregation.
    Both figures are stated because quoting the floor as the cost is the same
    error this module exists to stop. A caller asking for all-time gets all-time,
    over the bar, and is told so.
  - Sampling was considered and not adopted: at these figures it buys nothing a
    window does not, and its population statement is harder to read correctly.

The window is a PARAMETER, never a hidden constant — the third thing the
predecessor got wrong.

NEGATIVE-SPEC

  - Adjudicates nothing. This module supplies numbers; deciding an op's fate is
    `requirement-first-adjudication-06` and `ceremony-step-review-08`. No
    threshold comparison, no verdict, no roster, no suspension.
  - Not a general query surface. Claude-klabauter owns no general cockpit read surface
    (CLAUDE.md § What this repo is). Two questions, both named on the baton:
    what does an op cost, and over what population.
  - Never extends `op_budget_breaches.py`. That module says of itself that it is
    "a sibling of `op_census.report`, not an extension"; conflating them is how
    the restated-rather-than-imported duplication happened the first time. This
    module IMPORTS its sink reader rather than restating it.
  - Spawn counts are a FLOOR, not a total — `telemetry.spawn_counter` sees the
    git chokepoint only, so a private `subprocess.run` is invisible to it. Said
    on the row, said here, and said in the rendered output.

Spec backlink: state/handoffs/2026-08-25_roadmap-the-meter-02.md
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from coordinator_core.telemetry.op_latency import (
    BENCHMARK,
    PRODUCTION,
    TEST,
    UNKNOWN,
    _sink_path,
)

ORIGINS = (PRODUCTION, TEST, BENCHMARK, UNKNOWN)

#: Row kinds in the sink. `process_time` rows carry `process_ms` (CPU) and, since
#: the-meter-02, `spawns`. `complete` rows carry `elapsed_ms` (WALL CLOCK) and are
#: read here ONLY to count invocations — never to produce a duration, per this
#: baton's anti-scope ("Do not measure wall clock").
KIND_PROCESS_TIME = "process_time"
KIND_COMPLETE = "complete"


class PopulationIncomplete(RuntimeError):
    """A population that could not be read completely.

    Raised, never flagged. The predecessor's defect was disclosing an incomplete
    read in a field (`source.head_truncated`) that four sessions read past while
    quoting the short number as if it were whole. A count that cannot be complete
    fails loud — that is AC4, and it is the reason this is an exception type and
    not a boolean on the result.
    """


@dataclass(frozen=True)
class GenerationRead:
    """One sink generation, and exactly what was read out of it."""

    path: str
    bytes_read: int
    rows: int
    #: Lines that did not parse as JSON. A single trailing one is tolerated (the
    #: live tail can be mid-append when a reader arrives); anything else is a
    #: corrupt population and raises.
    unparseable: int


@dataclass(frozen=True)
class Population:
    """What a result was computed over — the answer's own provenance.

    Every field here exists so a reader cannot mistake a partial scan for a
    complete one (AC3). `complete` is always True on a returned result: an
    incomplete read raises rather than returning, so the field is a statement of
    the invariant, not a condition to branch on.
    """

    generations: Sequence[GenerationRead]
    window: str
    filters: Dict[str, object]
    rows: int
    complete: bool = True

    def describe(self) -> str:
        """One-line human statement of the population. Used in rendered output."""
        gens = ", ".join(
            f"{g.path} ({g.rows} rows, {g.bytes_read} bytes)" for g in self.generations
        )
        filt = ", ".join(f"{k}={v!r}" for k, v in sorted(self.filters.items())) or "none"
        return (
            f"window={self.window}; generations=[{gens}]; "
            f"filters={filt}; rows={self.rows}; complete={self.complete}"
        )


@dataclass
class OpMeasurement:
    """Per-op cost, split by origin so contamination is visible, never blended."""

    op: str
    #: Invocation counts keyed by origin — the field AC2 turns on.
    counts_by_origin: Dict[str, int] = field(default_factory=dict)
    process_ms: List[float] = field(default_factory=list)
    spawns: List[int] = field(default_factory=list)

    @property
    def production_count(self) -> int:
        """Invocations known to be production. Excludes `unknown` deliberately."""
        return self.counts_by_origin.get(PRODUCTION, 0)

    def summary(self) -> Dict[str, object]:
        """Reduce to the two axes the brightline is stated in, plus provenance.

        Percentiles are omitted below n=2 rather than computed — a p95 over one
        sample is a number that reads like a distribution and is not one, which
        is the same class of error as the 30016.6ms ceiling rows that D4 filed.
        """
        out: Dict[str, object] = {
            "op": self.op,
            "counts_by_origin": dict(self.counts_by_origin),
            "production_count": self.production_count,
            "process_time_samples": len(self.process_ms),
            "spawn_samples": len(self.spawns),
        }
        if self.process_ms:
            out["process_ms_min"] = round(min(self.process_ms), 1)
            out["process_ms_max"] = round(max(self.process_ms), 1)
            out["process_ms_mean"] = round(statistics.mean(self.process_ms), 1)
            if len(self.process_ms) > 1:
                out["process_ms_p50"] = round(statistics.median(self.process_ms), 1)
        if self.spawns:
            out["spawns_min"] = min(self.spawns)
            out["spawns_max"] = max(self.spawns)
            out["spawns_mean"] = round(statistics.mean(self.spawns), 1)
        return out


def generation_paths(repo_root: Path, *, window: str) -> List[Path]:
    """Sink generations to read, newest first, for the named window.

    ``window="current"`` reads only the live generation — the default, 62ms.
    ``window="all"`` reads every rotated generation too — 875ms, the honest
    all-time answer. The window is named on the result; there is no third,
    hidden option.
    """
    # Validated BEFORE any filesystem work: an unrecognised window is a caller
    # error, and resolving a git common dir first means it surfaces as "not a git
    # repository" instead — the wrong defect, named at the wrong layer.
    if window not in ("current", "all"):
        raise ValueError(f"unknown window {window!r} — expected 'current' or 'all'")

    from coordinator_core.lifecycle import git_common_dir

    sink = _sink_path(git_common_dir(repo_root))
    if window == "current":
        return [sink]
    # Review: coordinatorcode-reviewer — window is validated at function top, so
    # this is always "all" here; the trailing raise was unreachable dead code
    # duplicating the earlier message.
    rotated = sorted(
        sink.parent.glob(f"{sink.stem}.*.jsonl"),
        key=lambda p: p.name,
    )
    return [sink, *rotated]


def _read_generation(path: Path) -> tuple[List[dict], GenerationRead]:
    """Parse one generation whole. No tail bound, no row cap — see AC4.

    Tolerates exactly ONE unparseable trailing line: the sink is append-only and
    concurrently written, so a reader can legitimately arrive mid-append. Any
    other parse failure means the population is corrupt, and this raises rather
    than returning a quietly short one.

    "Trailing" means the last non-blank line read, not strictly the file's last
    byte: a blank line after a bad line is skipped before `last_line_bad` is
    touched, so `bad-line\\n\\n` still tolerates the bad line as trailing. This is
    a deliberate reading (blank lines are noise, not content), pinned by
    `test_bad_line_then_trailing_blank_line_is_still_tolerated`.
    """
    rows: List[dict] = []
    unparseable = 0
    last_line_bad = False
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], GenerationRead(str(path), 0, 0, 0)
    except OSError as exc:
        raise PopulationIncomplete(f"{path}: unreadable ({exc})") from exc

    # Streamed, never `read_bytes()`. The all-time window is 113MB across four
    # generations, and slurping it costs that much resident memory on a box
    # carrying 50-70 peers (CLAUDE.md § Load norm: "the load is us"). Iterating
    # the handle reads the same rows without ever holding the corpus.
    try:
        with path.open("rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                    last_line_bad = False
                except Exception:
                    unparseable += 1
                    last_line_bad = True
    except OSError as exc:
        raise PopulationIncomplete(f"{path}: unreadable ({exc})") from exc

    if unparseable > 1 or (unparseable == 1 and not last_line_bad):
        raise PopulationIncomplete(
            f"{path}: {unparseable} unparseable line(s) — the population cannot "
            f"be read completely, and a partial count is not returned. "
            f"(One unparseable TRAILING line is tolerated as a live append.)"
        )

    return rows, GenerationRead(str(path), size, len(rows), unparseable)


def measure(
    repo_root: Path,
    *,
    window: str = "current",
    ops: Optional[Iterable[str]] = None,
    origins: Optional[Iterable[str]] = None,
) -> tuple[Dict[str, OpMeasurement], Population]:
    """Per-op process time and spawn count over a stated population.

    Returns ``(measurements_by_op, population)``. The population is not optional
    and not a side channel: a caller that has the numbers necessarily has what
    they were computed over.

    Args:
        repo_root: worktree whose sink to read.
        window: ``"current"`` (default, ~62ms) or ``"all"`` (~875ms, all-time).
        ops: restrict to these op names. ``None`` reads every op.
        origins: restrict to these origins. ``None`` reads every origin and
            reports the split — which is what AC2 wants, since the interesting
            fact about `ping` is the SHAPE of its origin split, not a filtered
            subtotal.

    Raises:
        PopulationIncomplete: any generation could not be read whole.
    """
    op_filter = set(ops) if ops is not None else None
    origin_filter = set(origins) if origins is not None else None

    generations: List[GenerationRead] = []
    by_op: Dict[str, OpMeasurement] = {}
    total_rows = 0

    for path in generation_paths(repo_root, window=window):
        rows, read = _read_generation(path)
        generations.append(read)
        for row in rows:
            kind = row.get("kind", KIND_COMPLETE)
            if kind not in (KIND_PROCESS_TIME, KIND_COMPLETE):
                continue
            op = row.get("op")
            if not isinstance(op, str):
                continue
            if op_filter is not None and op not in op_filter:
                continue
            origin = row.get("origin") or UNKNOWN
            if origin_filter is not None and origin not in origin_filter:
                continue

            total_rows += 1
            m = by_op.get(op)
            if m is None:
                m = by_op[op] = OpMeasurement(op=op)
            # Count invocations off `complete` rows only. A `process_time` row is
            # a second record of the SAME invocation, so counting both double-counts
            # every op that records process time — the exact double-count
            # `double_routed_corr_ids` exists to make visible for routes.
            if kind == KIND_COMPLETE:
                m.counts_by_origin[origin] = m.counts_by_origin.get(origin, 0) + 1
            else:
                pms = row.get("process_ms")
                if isinstance(pms, (int, float)):
                    m.process_ms.append(float(pms))
                spawns = row.get("spawns")
                if isinstance(spawns, int):
                    m.spawns.append(spawns)

    population = Population(
        generations=tuple(generations),
        window=window,
        filters={
            "ops": sorted(op_filter) if op_filter else None,
            "origins": sorted(origin_filter) if origin_filter else None,
            # Review: coordinatorcode-reviewer — `started`/`composition` rows are
            # real narrowing dropped before total_rows is counted, and were the
            # unstated gap between GenerationRead.rows and Population.rows (the
            # same class of ambiguity AC3 exists to close; near-miss of D1/D-class
            # unfiltered-vs-filtered defects).
            "kind": [KIND_PROCESS_TIME, KIND_COMPLETE],
        },
        rows=total_rows,
    )
    return by_op, population


def render(
    measurements: Dict[str, OpMeasurement],
    population: Population,
    *,
    top: Optional[int] = None,
) -> Dict[str, object]:
    """Assemble the returned document: rows, population, and the spawn caveat.

    ``top`` bounds the RENDER, never the read — and when it drops rows it says
    how many. A silent top-N is how a partial answer starts reading as a
    complete one.
    """
    ordered = sorted(
        measurements.values(),
        key=lambda m: (sum(m.counts_by_origin.values()), max(m.process_ms, default=0.0)),
        reverse=True,
    )
    # Review: coordinatorcode-reviewer — `top` must be `is not None`-tested, not
    # truthiness-tested: `--top 0` is a valid, plausible "show none" request, and
    # truthiness treats it as "unset", silently returning every row.
    shown = ordered[:top] if top is not None else ordered
    doc: Dict[str, object] = {
        "population": population.describe(),
        "population_detail": {
            "window": population.window,
            "rows": population.rows,
            "complete": population.complete,
            "generations": [
                {"path": g.path, "rows": g.rows, "bytes": g.bytes_read}
                for g in population.generations
            ],
            "filters": population.filters,
        },
        "ops": [m.summary() for m in shown],
        "spawn_count_caveat": (
            "Spawn counts are a FLOOR for two reasons, both of which understate. "
            "(a) telemetry.spawn_counter observes the git chokepoint "
            "(coordinator_core.git.run.run_git) only, so any private "
            "subprocess.run is invisible to it. (b) a Python-keyed count is low "
            "against job accounting regardless: a CreateProcess-keyed census of "
            "the close ceremony's gate path found 8 Python-created processes "
            "against 16 counted by the job object, cause unresolved "
            "(state/audits/2026-08-25-close-ceremony-gate-path-caller-census.md). "
            "Never compare a count from this field against a job-accounted count."
        ),
        "origin_caveat": (
            "Origin 'unknown' means the row predates sink-side origin tagging. It "
            "is NOT production and must never be added to a production count."
        ),
    }
    if top is not None and len(ordered) > len(shown):
        doc["rows_not_shown"] = len(ordered) - len(shown)
    return doc


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(
        prog="python3 -m coordinator_core.op_census.meter",
        description="Per-op process time and spawn count over a stated population.",
    )
    parser.add_argument("--repo-root", default=".", type=Path)
    parser.add_argument(
        "--window",
        default="current",
        choices=("current", "all"),
        help="'current' reads the live generation (~62ms); 'all' reads every "
        "rotated generation (~875ms, all-time).",
    )
    parser.add_argument("--op", action="append", dest="ops", default=None)
    parser.add_argument("--origin", action="append", dest="origins", default=None)
    parser.add_argument("--top", type=int, default=None)
    args = parser.parse_args(argv)

    started = time.process_time()
    try:
        measurements, population = measure(
            args.repo_root, window=args.window, ops=args.ops, origins=args.origins
        )
    except PopulationIncomplete as exc:
        print(json.dumps({"error": "population_incomplete", "detail": str(exc)}, indent=2))
        return 1

    doc = render(measurements, population, top=args.top)
    # The meter reports its own cost in the same axis it reports everyone else's.
    # An instrument that cannot say what it costs has no standing to convict.
    doc["meter_process_ms"] = round((time.process_time() - started) * 1000.0, 1)
    print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
