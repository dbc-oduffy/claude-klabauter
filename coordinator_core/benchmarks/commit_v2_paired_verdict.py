"""Paired verdict reader for ceremony.commit_v2: the instrument that judges, not the one that finds.

Pairs each `phase_time` ledger row to the `per_op_handler` `process_time` row of the same op and
equal non-null `corr_id`, then prints a table and exactly one final VERDICT line. Stdlib only,
read-only, spawns nothing. Exit codes: 0 PASS, 1 BREACH, 2 INSUFFICIENT.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

OP = "ceremony.commit_v2"
SCOPE = "per_op_handler"
PHASE_FLOOR_MS = 15.6


def _read_rows(repo_root: Path):
    from coordinator_core.telemetry.op_latency import sink_generations

    for gen in sink_generations(repo_root):
        try:
            with open(gen, "rb") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("op") == OP:
                        yield row
        except OSError:
            continue


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


NO_CORR_ID = "phase_no_corr_id"
DUP_PHASE = "phase_duplicate_corr_id"
NO_HANDLER = "phase_no_handler_row"
NO_PROCESS_MS = "handler_no_process_ms"
NO_PHASE = "handler_no_phase_row"
NESTED = "phase_nested"


def _pair(repo_root: Path) -> tuple[list[dict], list[dict]]:
    """(paired rows, unpaired entries). Each unpaired entry is {reason, t_start, paths}; join is corr_id only."""
    phase: dict[str, dict] = {}
    handler: dict[str, dict] = {}
    unpaired: list[dict] = []

    def _miss(reason: str, row: dict) -> None:
        unpaired.append({"reason": reason, "t_start": _num(row.get("t_start")) or 0.0, "paths": row.get("paths")})

    for row in _read_rows(repo_root):
        kind = row.get("kind")
        is_phase = kind == "phase_time"
        is_handler = kind == "process_time" and row.get("measurement_scope") == SCOPE
        if not (is_phase or is_handler):
            continue
        dop = row.get("dispatch_op")
        if is_phase and isinstance(dop, str) and dop and dop != OP:
            _miss(NESTED, row)
            continue
        cid = row.get("corr_id")
        if not isinstance(cid, str) or not cid:
            if is_phase:
                _miss(NO_CORR_ID, row)
            continue
        if is_phase:
            if cid in phase:
                _miss(DUP_PHASE, phase[cid])
            phase[cid] = row
        else:
            handler[cid] = row
    paired = []
    for cid, prow in phase.items():
        hrow = handler.get(cid)
        if hrow is None:
            _miss(NO_HANDLER, prow)
            continue
        pms = _num(hrow.get("process_ms"))
        if pms is None:
            _miss(NO_PROCESS_MS, prow)
            continue
        phases = prow.get("phases_ms")
        paired.append(
            {
                "corr_id": cid,
                "t_start": _num(prow.get("t_start")) or _num(hrow.get("t_start")) or 0.0,
                "paths": prow.get("paths"),
                "process_ms": pms,
                "spawns": _num(hrow.get("spawns")),
                "total_ms": _num(prow.get("total_ms")),
                "phases_ms": dict(phases) if isinstance(phases, dict) else {},
            }
        )
    for cid, hrow in handler.items():
        if cid not in phase:
            _miss(NO_PHASE, hrow)
    paired.sort(key=lambda r: r["t_start"])
    return paired, unpaired


def _filter(rows: list[dict], since_epoch: Optional[float], paths_max: int) -> list[dict]:
    out = []
    for r in rows:
        if since_epoch is not None and r["t_start"] < since_epoch:
            continue
        if not isinstance(r["paths"], int) or r["paths"] > paths_max:
            continue
        out.append(r)
    return out


def unpaired_by_reason(unpaired: list[dict], since_epoch: Optional[float]) -> dict[str, int]:
    """Unpaired count per reason, restricted to rows at or after `since_epoch`."""
    out: dict[str, int] = {}
    for u in unpaired:
        if since_epoch is not None and u["t_start"] < since_epoch:
            continue
        out[u["reason"]] = out.get(u["reason"], 0) + 1
    return dict(sorted(out.items()))


def paired_rows(repo_root: Path, *, since_epoch: Optional[float], paths_max: int = 2) -> list[dict]:
    """Paired dispatches (phase_time + per_op_handler by corr_id), oldest first; unpaired excluded."""
    return _filter(_pair(Path(repo_root))[0], since_epoch, paths_max)


def _median_phases(rows: list[dict]) -> dict[str, float]:
    names: list[str] = []
    for r in rows:
        for n in r["phases_ms"]:
            if n not in names:
                names.append(n)
    out = {}
    for n in names:
        vals = [_num(r["phases_ms"].get(n)) or 0.0 for r in rows]
        out[n] = statistics.median(vals)
    return out


def verdict(
    rows: list[dict],
    *,
    spawn_baseline: float = 1.0,  # S1's median spawns per row (2026-10-23-commit-v2-handler-phase-attribution.md)
    bar_ms: float = 500.0,
    min_rows: int = 5,
) -> str:
    """The single VERDICT line. A paired set with no spawn counts cannot claim the spawn leg: BREACH."""
    n = len(rows)
    if n < min_rows:
        return f"VERDICT: INSUFFICIENT -- {n} paired rows"
    times = [r["process_ms"] for r in rows]
    med, mx = statistics.median(times), max(times)
    spawns = [r["spawns"] for r in rows if r.get("spawns") is not None]
    spawn_med = statistics.median(spawns) if spawns else None
    if med <= bar_ms and mx <= bar_ms and spawn_med is not None and spawn_med <= spawn_baseline:
        tag = "reduced" if spawn_med < spawn_baseline else "flat"
        return f"VERDICT: PASS -- median {med:.1f}ms max {mx:.1f}ms over {n} paired rows; spawns: {tag}"
    if med <= bar_ms and mx <= bar_ms:
        shown = "absent" if spawn_med is None else f"median {spawn_med:.1f} over baseline {spawn_baseline:.1f}"
        return f"VERDICT: BREACH -- spawns {shown}"
    phases = _median_phases(rows)
    if phases:
        name, ms = max(phases.items(), key=lambda kv: kv[1])
        return f"VERDICT: BREACH -- {name} {ms:.1f}ms"
    return f"VERDICT: BREACH -- median {med:.1f}ms max {mx:.1f}ms"


def _parse_since(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.1f}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--since", default=None)
    ap.add_argument("--paths-max", type=int, default=2)
    ap.add_argument("--min-rows", type=int, default=5)
    args = ap.parse_args(argv)
    since = _parse_since(args.since) if args.since else None
    allpaired, unpaired = _pair(Path(args.repo_root))
    rows = _filter(allpaired, since, args.paths_max)

    reasons = unpaired_by_reason(unpaired, since)
    nested = reasons.pop(NESTED, 0)
    print(f"unpaired: {sum(reasons.values())}")
    for reason, n in reasons.items():
        print(f"  {reason}: {n}")
    print(f"phase_nested: {nested}")
    print()
    print("| t_start | paths | process_ms | spawns | phase total_ms | phases >= 15.6ms |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        big = ", ".join(
            f"{n}={ms:.1f}" for n, ms in r["phases_ms"].items() if (_num(ms) or 0.0) >= PHASE_FLOOR_MS
        )
        print(
            f"| {_iso(r['t_start'])} | {r['paths']} | {_fmt(r['process_ms'])} | "
            f"{_fmt(r['spawns'])} | {_fmt(r['total_ms'])} | {big or '-'} |"
        )
    print()
    line = verdict(rows, min_rows=args.min_rows)
    print(line)
    if line.startswith("VERDICT: PASS"):
        return 0
    return 2 if line.startswith("VERDICT: INSUFFICIENT") else 1


if __name__ == "__main__":
    sys.exit(main())
