"""
coordinator_core.benchmarks.interleave -- Interleaved, randomised multi-primitive
benchmark sampler.

Purpose: `harness.run()` loops `target_ops` sequentially, N samples per op with
warm-up discard -- per-op-SEQUENTIAL, never interleaved or randomised across
primitives. That is fine for a single op's own SLA-conformance sweep, but it is
the wrong shape for a claim of the form "X is cheaper than Y on this box": a
block-sampled comparison (measure X ten times, THEN measure Y ten times) is
confounded by whatever the box's ambient load did in between the two blocks --
this repo runs 50-70 concurrent LLM sessions, and load drifts within seconds.
`run_interleaved()` is the fix: it round-robins across >=2 primitives in a
FRESH random shuffle per round, so every primitive's samples are drawn from
the same interleaved span of wall-clock time and ambient-load drift lands on
all of them roughly equally rather than biasing one.

Inexpressibility, not a warning -- but only of this module's entrypoint: this
module offers no lower-arity function that would let a caller draw N samples of
a single primitive and call it a benchmark; `run_interleaved()` raises
ValueError for `len(primitives) < 2` rather than silently permitting the
block-sampled shape. That claim is about the entrypoint's own surface, not
about `Primitive` generally: `Primitive` is a plain public dataclass with a
callable `invoke` field, and nothing stops a caller from looping
`primitive.invoke()` directly, outside this module, to reproduce exactly the
block-sampled measurement described above. Read "inexpressible" as "not
reachable through `run_interleaved()`", not as "impossible with this
module's types in hand". (Flagged by code review, 2026-08-16.)

Reuse, not re-authoring: sample reduction goes through
`coordinator_core.benchmarks.harness._percentile` (the same nearest-rank
percentile helper `harness.run()` uses for its own p50/p95/p99, imported
rather than re-implemented -- see that module's docstring for why nearest-rank
over `statistics.quantiles`). The `forwarder_dispatcher_roundtrip` baseline
primitive draws its samples through `coordinator_core.benchmarks.timer.
time_invocation` unmodified (a bare `ping` invocation -- the same draw
`floor.measure_floor` already takes, given a name here so it can be
interleaved against non-invoke primitives). The `stdlib_parent_walk` baseline
reuses `coordinator_core.ops._git_root_util.git_root_zero_spawn` rather than
re-implementing the upward `.git`-search walk.

What is new here (and could not be reused from elsewhere): a primitive in this
module's sense is an arbitrary zero-arg timed draw -- some are child-process
spawns (`bare_cpython_start`, `git_rev_parse_show_toplevel`), one is an
in-process pure-Python call (`stdlib_parent_walk`), and one goes through the
invoke entrypoint (`forwarder_dispatcher_roundtrip`). `timer.time_invocation`
only knows how to time the invoke entrypoint's fixed argv shape, so it cannot
serve as the timing primitive for the other three; `_time_subprocess` and
`_time_callable` below are the minimal generic wrappers that make all four
primitives interleavable through one uniform `Primitive.invoke() -> float`
shape.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C1.
"""

from __future__ import annotations

import random
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from coordinator_core.benchmarks.harness import _percentile
from coordinator_core.benchmarks.timer import (
    SUBPROCESS_CREATIONFLAGS,
    SUBPROCESS_TIMEOUT_S,
    time_invocation,
)
from coordinator_core.ops._git_root_util import git_root_zero_spawn


@dataclass(frozen=True)
class Primitive:
    """One named, zero-arg timed draw. `invoke()` performs ONE sample and
    returns its elapsed wall time in milliseconds; it is called fresh for
    every draw in a `run_interleaved()` round-robin, never batched."""

    name: str
    invoke: Callable[[], float]


@dataclass(frozen=True)
class PrimitiveStats:
    """Reduced median/p90 summary for one primitive's draws within a single
    `run_interleaved()` call. `samples_ms` is the raw per-draw sequence in
    the order it was collected (interleaved with the other primitives'
    draws, not contiguous) -- kept for callers that want their own
    reduction on top of median/p90."""

    name: str
    sample_count: int
    median_ms: float
    p90_ms: float
    samples_ms: List[float]


def run_interleaved(
    primitives: Sequence[Primitive],
    n: int,
    rng: Optional[random.Random] = None,
) -> Dict[str, PrimitiveStats]:
    """Round-robins `n` rounds across `primitives`, each round independently
    shuffled, and returns one `PrimitiveStats` per primitive name.

    Refuses (ValueError) rather than degrading: `len(primitives) < 2` (a
    single primitive cannot be interleaved against anything -- see module
    docstring), `n < 1`, or duplicate primitive names (stats would silently
    merge two distinct draws under one name).

    Each round is `rng.shuffle`'d independently -- draws are NOT grouped by
    primitive at any point, so no contiguous block of same-primitive samples
    can appear except by chance in an individual round's shuffle output
    bordering the next round's.
    """
    if len(primitives) < 2:
        raise ValueError(
            f"run_interleaved: need >=2 primitives to interleave, got {len(primitives)!r} "
            "-- a single-primitive block-sampled measurement is not expressible through "
            "this API"
        )
    if n < 1:
        raise ValueError(f"run_interleaved: n must be >= 1, got {n!r}")
    names = [p.name for p in primitives]
    if len(set(names)) != len(names):
        raise ValueError(f"run_interleaved: duplicate primitive names in {names!r}")

    rng = rng if rng is not None else random.Random()

    draw_order: List[Primitive] = []
    for _ in range(n):
        round_ = list(primitives)
        rng.shuffle(round_)
        draw_order.extend(round_)

    samples: Dict[str, List[float]] = {p.name: [] for p in primitives}
    for primitive in draw_order:
        samples[primitive.name].append(primitive.invoke())

    stats: Dict[str, PrimitiveStats] = {}
    for primitive in primitives:
        collected = samples[primitive.name]
        sorted_samples = sorted(collected)
        stats[primitive.name] = PrimitiveStats(
            name=primitive.name,
            sample_count=len(collected),
            median_ms=_percentile(sorted_samples, 50),
            p90_ms=_percentile(sorted_samples, 90),
            samples_ms=collected,
        )
    return stats


def _time_subprocess(argv: List[str], cwd: Optional[str] = None) -> float:
    """Generic spawn-to-exit timer for an arbitrary argv -- the sibling
    `timer.time_invocation` is hardcoded to the invoke entrypoint's argv
    shape and JSON-RPC error-envelope check, neither of which applies to a
    bare `python -c pass` or `git rev-parse` child process."""
    start = time.perf_counter()
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=SUBPROCESS_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if completed.returncode != 0:
        raise RuntimeError(
            f"interleave._time_subprocess: {argv!r} exited {completed.returncode}: "
            f"{completed.stderr[:500]!r}"
        )
    return elapsed_ms


def _time_callable(fn: Callable[[], object]) -> float:
    """Generic in-process timer for a pure-Python zero-arg callable (e.g.
    `stdlib_parent_walk`, which never spawns a subprocess at all)."""
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def default_baseline_primitives(repo_root: Optional[str] = None) -> List[Primitive]:
    """The four primitives this plan's claims rest on, baselined on THIS box:
    bare CPython start, a forwarder+dispatcher round trip (the `ping` op),
    `git rev-parse --show-toplevel`, and the zero-spawn stdlib parent walk
    that resolves the same answer without a subprocess.

    `repo_root` anchors the git-rooted primitives; defaults to the enclosing
    repo's toplevel via `git_root_zero_spawn` itself (a one-time, non-timed
    resolution at primitive-construction time -- never inside a timed draw).
    """
    resolved_root = repo_root if repo_root is not None else git_root_zero_spawn(Path(__file__))
    if resolved_root is None:
        raise RuntimeError(
            "interleave.default_baseline_primitives: could not resolve a repo root "
            "to anchor the git-rooted baseline primitives"
        )

    return [
        Primitive(
            name="bare_cpython_start",
            invoke=lambda: _time_subprocess([sys.executable, "-c", "pass"]),
        ),
        Primitive(
            name="forwarder_dispatcher_roundtrip",
            invoke=lambda: time_invocation("ping", "{}", None),
        ),
        Primitive(
            name="git_rev_parse_show_toplevel",
            invoke=lambda: _time_subprocess(
                ["git", "rev-parse", "--show-toplevel"], cwd=resolved_root
            ),
        ),
        Primitive(
            name="stdlib_parent_walk",
            invoke=lambda: _time_callable(
                lambda: git_root_zero_spawn(Path(resolved_root))
            ),
        ),
    ]


if __name__ == "__main__":  # pragma: no cover
    results = run_interleaved(default_baseline_primitives(), n=10)
    for name, stat in results.items():
        print(f"{name}: median={stat.median_ms:.2f}ms p90={stat.p90_ms:.2f}ms n={stat.sample_count}")
    sys.exit(0)
