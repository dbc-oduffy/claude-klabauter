"""Process-time, spawn and output-digest profile of the `review_tripwires` test scanners.

Runs `literal_namers`, `tree_scanners` and their one-read union `scan_tracked` over a repo's tracked tests, with inputs derived from
that repo's own git index so the same call is repeatable at any sha. Process time is this
process's CPU plus descendants (`commit_leg_profile._ChildMeter`); wall clock is never recorded.
One JSON-able row per scanner: `{axis, process_time_ms, spawns, reads, candidates, digest}`.
`spawns` counts `subprocess.Popen` audit events and `reads` counts `open` audit events inside
the measured call. `digest` is the sha256 of the scanner's canonical JSON output, so a changed
read strategy proves unchanged results by equal digests. `process_time_ms` is the median of
`--repeat` runs. A row over `BAR_MS` is the DR-344 kill gate.

    python coordinator_core/benchmarks/review_tripwires_scan_profile.py [--repo PATH]
        [--named N] [--created N] [--repeat N] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Sequence

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402
from coordinator_core.git import git_state  # noqa: E402
from coordinator_core.ops import review_tripwires as rt  # noqa: E402

BAR_MS = 500.0
DEFAULT_NAMED = 200
DEFAULT_CREATED = 50
DEFAULT_REPEAT = 3
_SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn"})


@dataclass
class Row:
    axis: str
    process_time_ms: float
    spawns: int
    reads: int
    candidates: int
    digest: str
    over_bar: bool


class _Counter:
    """Audit-hook tally of spawn and open events while `armed`."""

    def __init__(self) -> None:
        self.armed = False
        self.spawns = 0
        self.reads = 0
        sys.addaudithook(self._hook)

    def _hook(self, event: str, args: tuple) -> None:
        if not self.armed:
            return
        if event in _SPAWN_EVENTS:
            self.spawns += 1
        elif event == "open":
            self.reads += 1


_COUNTER: _Counter | None = None


def _counter() -> _Counter:
    global _COUNTER
    if _COUNTER is None:
        _COUNTER = _Counter()
    return _COUNTER


def derive_inputs(repo: Path, named: int, created: int) -> tuple[List[str], List[str]]:
    """Deterministic `named` and `created` path lists: the first N sorted tracked non-test `.py`
    paths under `coordinator_core/ops`, falling back to any tracked non-test `.py`."""
    tracked = sorted(
        p for p in git_state.read_index_paths(repo) if p.endswith(".py") and not rt._is_test_name(p.rsplit("/", 1)[-1])
    )
    pool = [p for p in tracked if p.startswith("coordinator_core/ops/")] or tracked
    return pool[:named], pool[-created:] if created else []


def digest_of(output) -> str:
    """sha256 of the canonical JSON form of a scanner's output."""
    return hashlib.sha256(json.dumps(output, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _measure(call: Callable[[], object], repeat: int) -> tuple[float, int, int, object]:
    counter = _counter()
    times: List[float] = []
    spawns = reads = 0
    output = None
    for i in range(max(repeat, 1)):
        meter = _ChildMeter()
        meter.start()
        t0 = time.process_time()
        counter.spawns = counter.reads = 0
        counter.armed = True
        try:
            result = call()
        finally:
            counter.armed = False
        own = (time.process_time() - t0) * 1000.0
        times.append(own + meter.stop_ms())
        if i == 0:
            spawns, reads, output = counter.spawns, counter.reads, result
    return round(statistics.median(times), 3), spawns, reads, output


def profile(repo: Path, *, named: int = DEFAULT_NAMED, created: int = DEFAULT_CREATED,
            repeat: int = DEFAULT_REPEAT) -> List[Row]:
    """Rows for `literal_namers` then `tree_scanners` over `repo`."""
    repo = Path(repo)
    named_paths, created_paths = derive_inputs(repo, named, created)
    candidates = sum(1 for _ in rt._tracked_test_bytes(repo, ()))
    rows: List[Row] = []
    for axis, call in (
        ("literal_namers", lambda: rt.literal_namers(repo, named_paths)),
        ("tree_scanners", lambda: rt.tree_scanners(repo, created_paths)),
        ("scan_tracked", lambda: rt.scan_tracked(repo, named_paths + created_paths, created_paths)),
    ):
        ms, spawns, reads, output = _measure(call, repeat)
        rows.append(Row(axis, ms, spawns, reads, candidates, digest_of(output), ms > BAR_MS))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=str(_REPO))
    ap.add_argument("--named", type=int, default=DEFAULT_NAMED)
    ap.add_argument("--created", type=int, default=DEFAULT_CREATED)
    ap.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    rows = profile(Path(args.repo), named=args.named, created=args.created, repeat=args.repeat)
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2))
    else:
        for r in rows:
            print(f"{r.axis}: process_time_ms={r.process_time_ms} spawns={r.spawns} reads={r.reads} "
                  f"candidates={r.candidates} digest={r.digest}{' OVER_BAR' if r.over_bar else ''}")
    return 1 if any(r.over_bar for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
