"""Census of `dispatch.land_run` process-time rows over the DR-344 500ms bar, attributed per leg.

Reads `kind == "process_time"` rows for op `dispatch.land_run` from the op-latency sink (the sink and
its rotated generations, resolved through `op_latency.sink_generations`, never a literal path) and joins
each over-bar row to its run's `land-legs.json` (`land_leg_record.write`) to name the legs that spent it.

Join key (established from what is persisted): a process_time row carries `t_start`, `corr_id`, `sid`
and `process_ms`, but NO run_id; `land-legs.json` carries legs and outcome but NO timestamp or corr_id.
The only shared quantity is time. The row's end time is `t_start + elapsed_ms/1000`, where `elapsed_ms`
comes from the `kind == "complete"` row with the same `corr_id`; the record file is written just before
the reply, so the file whose mtime lies in `[t_start - 0.5, t_end + SLACK_SECS]` and is nearest `t_end` is the
row's record. Each file is claimed once (rows are matched in `t_start` order). A row with no complete
row, no file in the window, or whose file a nearer row claimed is `unjoined`: counted, never dropped, so
`over_bar_rows` is always the sink total. A run landed twice overwrites its file, leaving the earlier
row unjoined. Only landed or halted `mode == product` calls write a record at all.

Per leg (legs are thread CPU, `land_deadline._legs_elapsed`): `rows_over_bar` is the joined over-bar
rows where that leg alone exceeds the bar; `dominant_rows` where it is the costliest leg; `max_ms` and
`p50_ms` run over its cost across every joined over-bar row that reached it. `unattributed` is the part
of a row's `process_ms` no leg carries (before `land_run.script_read` and after the last leg), floored
at zero. Rows of `origin` test or benchmark are not production and are skipped.

    python coordinator_core/benchmarks/land_run_over_bar_census.py [--json] [--since X] [--until X]
        [--repo-root PATH]

`--since`/`--until` take epoch seconds, an ISO timestamp, or `<N>h`/`<N>m`/`<N>d` ago; default window is
the last 24h. Output keys: `op`, `window`, `bar_ms`, `process_rows`, `over_bar_rows`, `joined`, `unjoined`,
`max_process_ms`, `outcomes`, `legs`, `unattributed`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

OP = "dispatch.land_run"
BAR_MS = 500.0
SLACK_SECS = 2.0
DEFAULT_WINDOW_SECS = 24 * 3600.0
_NON_PRODUCTION = frozenset({"test", "benchmark"})
_SPAN = re.compile(r"^(\d+(?:\.\d+)?)([hmd])$")
_UNIT = {"m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_time(text: str, now: float) -> float:
    """Epoch seconds from epoch digits, `<N>h|m|d` ago, or an ISO timestamp (naive means local)."""
    text = text.strip()
    span = _SPAN.match(text)
    if span:
        return now - float(span.group(1)) * _UNIT[span.group(2)]
    try:
        return float(text)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text)
    return parsed.timestamp() if parsed.tzinfo else parsed.astimezone().timestamp()


def read_rows(paths: list, since: float, until: float) -> tuple:
    """`(process_rows, elapsed_by_corr)` for `dispatch.land_run` in `[since, until]`, production only."""
    process_rows: list = []
    elapsed: dict = {}
    for path in paths:
        try:
            if os.stat(path).st_mtime < since:
                continue
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if OP not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("op") != OP:
                    continue
                if row.get("origin") in _NON_PRODUCTION:
                    continue
                t_start = row.get("t_start")
                if not isinstance(t_start, (int, float)) or not since <= t_start <= until:
                    continue
                kind = row.get("kind")
                if kind == "process_time" and isinstance(row.get("process_ms"), (int, float)):
                    process_rows.append(row)
                elif kind == "complete" and isinstance(row.get("elapsed_ms"), (int, float)):
                    if row.get("corr_id"):
                        elapsed[row["corr_id"]] = float(row["elapsed_ms"])
    return process_rows, elapsed


def record_files(root: Path) -> list:
    """`[(mtime, path)]` of every `land-legs.json` under `state/warp` and `scratch/warp`, sorted."""
    from coordinator_core.ops.dispatch_emit.land_leg_record import FILE_NAME

    found: list = []
    for top in ("state/warp", "scratch/warp"):
        try:
            entries = list(os.scandir(Path(root) / top))
        except OSError:
            continue
        for entry in entries:
            candidate = os.path.join(entry.path, FILE_NAME)
            try:
                found.append((os.stat(candidate).st_mtime, candidate))
            except OSError:
                continue
    found.sort()
    return found


def _load(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and isinstance(body.get("legs"), list) else None


def join_rows(over_rows: list, elapsed_by_corr: dict, files: list) -> tuple:
    """`([(row, record)], unjoined_rows)`: each file claimed by at most one row, nearest its end time."""
    claimed: set = set()
    joined: list = []
    unjoined: list = []
    for row in sorted(over_rows, key=lambda r: r["t_start"]):
        elapsed_ms = elapsed_by_corr.get(row.get("corr_id"))
        if elapsed_ms is None:
            unjoined.append(row)
            continue
        t_start = row["t_start"]
        t_end = t_start + elapsed_ms / 1000.0
        best = None
        for mtime, path in files:
            if path in claimed or mtime < t_start - 0.5 or mtime > t_end + SLACK_SECS:
                continue
            if best is None or abs(mtime - t_end) < abs(best[0] - t_end):
                best = (mtime, path)
        record = _load(best[1]) if best else None
        if record is None:
            unjoined.append(row)
            continue
        claimed.add(best[1])
        joined.append((row, record))
    return joined, unjoined


def _p50(values: list) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def census(*, repo_root: Path, since: float, until: float) -> dict:
    """The census dict over `[since, until]`; keys listed in the module docstring."""
    from coordinator_core.telemetry import op_latency

    paths = op_latency.sink_generations(repo_root)
    rows, elapsed = read_rows(paths, since, until)
    over = [r for r in rows if r["process_ms"] > BAR_MS]
    from coordinator_core.lifecycle import main_worktree_root

    joined, unjoined = join_rows(over, elapsed, record_files(main_worktree_root(repo_root)))

    per_leg: dict = {}
    unattributed: list = []
    outcomes: dict = {}
    for row, record in joined:
        outcomes[str(record.get("outcome"))] = outcomes.get(str(record.get("outcome")), 0) + 1
        costs: dict = {}
        for leg in record["legs"]:
            if isinstance(leg, dict) and isinstance(leg.get("process_time_ms"), (int, float)):
                costs[str(leg.get("leg"))] = costs.get(str(leg.get("leg")), 0.0) + float(leg["process_time_ms"])
        for name, ms in costs.items():
            per_leg.setdefault(name, {"ms": [], "over": 0, "dominant": 0})["ms"].append(ms)
            if ms > BAR_MS:
                per_leg[name]["over"] += 1
        if costs:
            per_leg[max(costs, key=costs.get)]["dominant"] += 1
        unattributed.append(max(0.0, row["process_ms"] - sum(costs.values())))

    legs = sorted(
        (
            {
                "leg": name,
                "rows_over_bar": data["over"],
                "dominant_rows": data["dominant"],
                "rows": len(data["ms"]),
                "max_ms": round(max(data["ms"]), 1),
                "p50_ms": _p50(data["ms"]),
            }
            for name, data in per_leg.items()
        ),
        key=lambda leg: (-leg["dominant_rows"], -leg["max_ms"], leg["leg"]),
    )
    return {
        "op": OP,
        "window": {"since": since, "until": until},
        "bar_ms": BAR_MS,
        "process_rows": len(rows),
        "over_bar_rows": len(over),
        "joined": len(joined),
        "unjoined": len(unjoined),
        "max_process_ms": round(max((r["process_ms"] for r in over), default=0.0), 1),
        "outcomes": outcomes,
        "legs": legs,
        "unattributed": {
            "rows": len(unattributed),
            "max_ms": round(max(unattributed, default=0.0), 1),
            "p50_ms": _p50(unattributed),
        },
    }


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--repo-root", default=str(_REPO))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = time.time()
    until = parse_time(args.until, now) if args.until else now
    since = parse_time(args.since, now) if args.since else until - DEFAULT_WINDOW_SECS
    result = census(repo_root=Path(args.repo_root), since=since, until=until)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")  # noqa: E731
    print(f"{OP} over {BAR_MS:.0f}ms: {result['over_bar_rows']} rows of {result['process_rows']} "
          f"({stamp(since)}..{stamp(until)}), joined {result['joined']}, unjoined {result['unjoined']}")
    for leg in result["legs"]:
        print(f"  {leg['leg']:<44} over={leg['rows_over_bar']:<4} dom={leg['dominant_rows']:<4} "
              f"max={leg['max_ms']:<9} p50={leg['p50_ms']}")
    print(f"  unattributed max={result['unattributed']['max_ms']} p50={result['unattributed']['p50_ms']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
