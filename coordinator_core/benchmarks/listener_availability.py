"""
coordinator_core.benchmarks.listener_availability — sample p(listener up) for the
warm-engine HTTP hook listener, from outside, without ever starting it.

Purpose: `budget-manifest.json` §`_hook_seam_http_transport.c4_supervised_listener.
p_listener_up` carries `value: None` and a `what_would_close_it` note. This module IS
that closure: it reports the fraction of samples in which the HTTP listener was up AND
answering, with its n and its window, so a `type: "http"` hook registration can be
argued for or against on a measured availability number rather than an assumed one.

The number matters because Claude Code's `type: "http"` hook FAILS OPEN: when nothing
answers, the harness does not fire the hook at all. A registration therefore trades the
cold path's ~27-32ms child process for the hook not running at all during every outage
window — for a blocking guard that is a safety regression, for an advisory it is a lost
advisory. Neither can be sized without this fraction.

WHY IT MUST BE RUN AGAINST THE STAMPED CLONE. `supervisor.ensure_listener`'s
`is_engine_root` gate refuses an unstamped tree by design (DR-315 s2, "published engine
or nothing"), so p(listener up) sampled from this dev working tree is structurally 0.0
and says nothing about production — that is the `n=10, value=0.0` row already in the
manifest, correct-by-design rather than a finding. `--engine-root` therefore defaults to
`coordinator_core.warm.engine_root.current_engine_clone()` (the resolved *published*
clone) and the sampler refuses to run against a root that `is_engine_root` rejects,
rather than silently collecting a column of zeros that reads like an availability
finding.

OBSERVATION MUST NOT DISTURB THE SUBJECT. Three things this sampler never does:

- **Never `ensure_listener`.** That function's whole job is to START a listener on miss.
  A sampler that calls it measures its own side effect and reports ~1.0 forever.
- **Never the C door (`coordinator-invoke.exe`).** On any doubt the door falls straight
  through to cold Python and propagates its exit code, printing nothing — a dead listener
  and a live one produce the same visible success, so a door-based probe reads "up" even
  when the subject is stone dead. (DoE-claude
  `docs/research/2026-08-25-http-listener-availability.md` § (b), reached independently
  for the pipe transport.)
- **Never POST /hook.** The hook path runs real guard dispatch; sampling it would put
  synthetic guard traffic on a resident server serving dozens of sessions. `GET /health`
  is the read-only arm and the one `supervisor.check_health` already exists for.

FOUR OUTCOMES, not two. "Down" is not one failure mode, and collapsing them loses the
finding — the 2026-08-25 pipe-server measurement found its 31.5-minute outage was a
percolate that deleted the build stamp, visible only because record-absent and
record-present-but-dead were distinguishable:

    up            record live AND GET /health answers 2xx
    unhealthy     record live (pid still the process that wrote it) but /health silent
                  -- a WEDGED listener, the one failure a pid check alone reads as up
    dead_record   record present, pid gone -- crashed without unlinking discovery
    no_record     no discovery record at all -- never started, or cleanly torn down

`up` is the numerator of p(listener up). Everything else is an outage window for a
fail-open hook, whatever its cause.

Standalone: `python -m coordinator_core.benchmarks.listener_availability --sample`
            `python -m coordinator_core.benchmarks.listener_availability --report`

A minimum interval floor (10s) is enforced so the sampler cannot itself become a load
source, matching `ambient_sampler`'s own floor. Ctrl-C-clean.

Spec backlink: state/handoffs/2026-08-25-the-hook-registration-waits-on-a-decision-record.md
               docs/plans/2026-08-23-no-hook-fire-pays-an-interpreter-start.md row H6
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.warm import supervisor
from coordinator_core.warm.engine_root import current_engine_clone, is_engine_root

MIN_INTERVAL_SECONDS = 10.0
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_SINK = Path("state/measurements/http-listener-availability.jsonl")

UP = "up"
UNHEALTHY = "unhealthy"
DEAD_RECORD = "dead_record"
NO_RECORD = "no_record"


def sample_once(engine_root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    """One outcome row. Never raises, never spawns, never mutates the subject."""
    t = time.time() if now is None else now
    row: Dict[str, Any] = {"t": t, "outcome": NO_RECORD, "port": None, "pid": None}

    try:
        record = supervisor.read_discovery(engine_root)
    except Exception as exc:  # noqa: BLE001 -- a sampler outlives whatever it reads
        row["outcome"] = NO_RECORD
        row["error"] = "%s: %s" % (type(exc).__name__, exc)
        return row

    if not record:
        return row

    row["port"] = record.get("port")
    row["pid"] = record.get("pid")
    row["started_at"] = record.get("started_at")

    if not supervisor.discovery_is_live(record):
        row["outcome"] = DEAD_RECORD
        return row

    url = supervisor.listener_url(record)
    if not url:
        row["outcome"] = DEAD_RECORD
        return row

    started = time.perf_counter()
    healthy = supervisor.check_health(url)
    row["probe_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    row["outcome"] = UP if healthy else UNHEALTHY
    return row


def _append(sink: Path, row: Dict[str, Any]) -> None:
    sink.parent.mkdir(parents=True, exist_ok=True)
    with sink.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_rows(sink: Path) -> List[Dict[str, Any]]:
    if not sink.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in sink.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "outcome" in obj:
            rows.append(obj)
    return rows


def report(sink: Path) -> Dict[str, Any]:
    """p(listener up) with its n, its window, and the outage breakdown that produced it.

    Outages are reported as CONSECUTIVE RUNS as well as a count, because a fail-open hook
    cares about the length of the window in which it silently did not fire, not about how
    many samples fell inside it.
    """
    rows = _read_rows(sink)
    n = len(rows)
    out: Dict[str, Any] = {"n": n, "sink": str(sink)}
    if not n:
        out["p_listener_up"] = None
        out["why"] = "no samples collected"
        return out

    counts: Dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome"))
        counts[outcome] = counts.get(outcome, 0) + 1

    ups = counts.get(UP, 0)
    out["p_listener_up"] = round(ups / n, 4)
    out["outcomes"] = counts
    out["window_start"] = rows[0].get("t")
    out["window_end"] = rows[-1].get("t")
    out["window_hours"] = round((rows[-1].get("t", 0) - rows[0].get("t", 0)) / 3600.0, 3)

    runs: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for row in rows:
        if row.get("outcome") == UP:
            current = None
            continue
        if current is None:
            current = {"outcome": row.get("outcome"), "start": row.get("t"), "end": row.get("t"), "samples": 1}
            runs.append(current)
        else:
            current["end"] = row.get("t")
            current["samples"] += 1
    out["outage_runs"] = [
        {
            "outcome": run["outcome"],
            "samples": run["samples"],
            "minutes": round((run["end"] - run["start"]) / 60.0, 2),
        }
        for run in runs
    ]
    out["longest_outage_minutes"] = max(
        (r["minutes"] for r in out["outage_runs"]), default=0.0
    )

    probes = [r["probe_ms"] for r in rows if isinstance(r.get("probe_ms"), (int, float))]
    if probes:
        probes_sorted = sorted(probes)
        out["probe_ms"] = {
            "n": len(probes_sorted),
            "min": probes_sorted[0],
            "p50": probes_sorted[len(probes_sorted) // 2],
            "max": probes_sorted[-1],
        }
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", action="store_true", help="collect samples until interrupted")
    parser.add_argument("--report", action="store_true", help="report p(listener up) from collected samples")
    parser.add_argument("--once", action="store_true", help="take exactly one sample and print it")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--engine-root", default=None, help="defaults to the resolved published engine clone")
    parser.add_argument("--sink", default=str(DEFAULT_SINK))
    args = parser.parse_args(argv)

    sink = Path(args.sink)

    if args.report:
        print(json.dumps(report(sink), indent=2))
        return 0

    root = Path(args.engine_root) if args.engine_root else Path(current_engine_clone())
    if not is_engine_root(root):
        print(
            "refusing to sample %s: not a published engine clone. p(listener up) there is "
            "structurally 0.0 by the is_engine_root gate, which is a property of the clone, "
            "not an availability finding." % root,
            file=sys.stderr,
        )
        return 2

    if args.once:
        print(json.dumps(sample_once(root)))
        return 0

    if not args.sample:
        parser.print_help()
        return 1

    interval = max(float(args.interval), MIN_INTERVAL_SECONDS)
    print("sampling %s every %.0fs -> %s" % (root, interval, sink), file=sys.stderr)
    try:
        while True:
            _append(sink, sample_once(root))
            time.sleep(interval)
    except (KeyboardInterrupt, SystemExit):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
