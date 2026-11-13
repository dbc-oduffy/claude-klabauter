"""Warm-engine saturation measurement: process time and spawns against the 500ms bar.

Naked stdlib Python 3.11+, Windows/macOS/Linux, no bash, no git spawn. Run as
`python -m coordinator_core.benchmarks.warm_saturation_measure`. Answers, for a
window of LIVE traffic on the box it runs on:

  * per-op process time (p50/p95/p99/max) and spawn count, against the 500ms bar
    and the 200ms fix bar -- read from the engine's own op-latency rows, which the
    warm pool worker stamps with `process_ms` and `spawns` for every real call;
  * which guard legs carry the slow Bash/Write hooks (`legs` on per_op_handler rows;
    the writer floors legs at 50ms and times thread CPU only, so child-process time
    is not in a leg);
  * pool occupancy, reconstructed: a pool worker serves one request at a time, so
    the number of distinct worker pids with an open started->complete interval is
    the occupied-slot count (gate = 4 heavy + 2 reserve; hook lane cap 3);
  * door-deadline misses: hook ops whose wall time passed the 8s hook-mode door
    deadline (wall is SECONDARY -- reported only to name a miss, never as a cost);
  * stall dumps and hang restarts written in the window;
  * optionally (--probe N), a serial warm `session.whoami_live` dial: one request in
    flight at a time, never more, so the probe cannot itself fill the 3-slot hook
    lane on a loaded box. Needs the engine checkout importable (--engine-root).

Passive by default: it adds no engine traffic. Paste the JSON block it prints.

Session coverage: `sid` is stamped by `coordinator_core.telemetry.op_latency`
(`record_op_started` and `record_op_latency` for the started/complete rows, fed
from `coordinator_core.ipc`; process_time rows get it from `ipc._telemetry_sid`).
A row without `sid` is counted in `rows_without_sid`; a low
`distinct_sessions_in_rows` beside a high `rows_without_sid` means the writer
did not resolve a session, not that the box is quiet.

Trap: the op-latency sink is per repo (<hub>/logs/op-latency.jsonl, hub per
session_hub_path.hub_for_common) while the engine is box-wide. Read every repo that had traffic
(--scan-root) or occupancy is under-counted.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import platform
import sys
import time
from pathlib import Path

PROCESS_BAR_MS = 500.0
FIX_BAR_MS = 200.0
DOOR_HOOK_DEADLINE_MS = 8000.0
HOOK_PREFIXES = ("hooks.", "session.", "warm_guard.", "bash_guards.")
GATE_HEAVY_SLOTS = 4  # warm/server.py _DISPATCH_POOL_IO_CAP (min with cpu_count)
GATE_TOTAL_SLOTS = 6  # + _LIGHT_RESERVE_WORKERS

_SPAWN_EVENTS = {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.startfile"}
_spawns = 0


def _audit(event: str, _args) -> None:
    global _spawns
    if event in _SPAWN_EVENTS:
        _spawns += 1


def _common_dir(repo: Path) -> Path | None:
    """Resolve <repo>'s git common dir from files alone (no git spawn)."""
    dot = repo / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        try:
            line = dot.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if not line.startswith("gitdir:"):
            return None
        gd = Path(line[7:].strip())
        gd = gd if gd.is_absolute() else (repo / gd)
        cd = gd / "commondir"
        if cd.is_file():
            rel = Path(cd.read_text(encoding="utf-8", errors="replace").strip())
            return (rel if rel.is_absolute() else gd / rel).resolve()
        return gd
    return None


def _sinks(repos: list[Path], scan_root: Path | None) -> list[Path]:
    from coordinator_core.session_hub_path import hub_for_common

    cands = list(repos)
    if scan_root is not None and scan_root.is_dir():
        cands += [p for p in scan_root.iterdir() if p.is_dir()]
    out, seen = [], set()
    for r in cands:
        cd = _common_dir(r)
        if cd is None:
            continue
        s = hub_for_common(cd) / "logs" / "op-latency.jsonl"
        key = str(s.resolve()) if s.exists() else None
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _read_rows(sink: Path, cutoff: float, tail_bytes: int) -> list[dict]:
    """Op-latency rows with `t_start >= cutoff` from the last `tail_bytes` of `sink`."""
    rows = []
    try:
        size = sink.stat().st_size
        with open(sink, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # drop the partial first line
            for line in fh:
                if b'"t_start"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                t = r.get("t_start")
                if isinstance(t, (int, float)) and t >= cutoff:
                    r["_sink"] = sink.parents[2].name if sink.parents[2].name not in (".git", ".coordinator-local") else sink.parents[3].name
                    rows.append(r)
    except OSError:
        pass
    return rows


def _pct(vals: list[float], q: float):
    if not vals:
        return None
    v = sorted(vals)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)


def _op_table(rows: list[dict]) -> dict:
    proc = collections.defaultdict(list)
    spawn = collections.defaultdict(list)
    wall = collections.defaultdict(list)
    outcomes = collections.defaultdict(collections.Counter)
    started = collections.defaultdict(set)
    completed = set()
    for r in rows:
        if r.get("origin") not in (None, "production"):
            continue
        op, kind = r.get("op"), r.get("kind")
        key = f"{op} [{r.get('route')}]"
        # One call writes two process_time rows (per_op_process, per_op_handler);
        # count per_op_process only; per_op_handler rows are skipped here and read by _slow_legs.
        if kind == "process_time" and r.get("process_ms") is not None and r.get("measurement_scope") != "per_op_handler":
            proc[key].append(float(r["process_ms"]))
            spawn[key].append(int(r.get("spawns") or 0))
        elif kind == "complete" and r.get("elapsed_ms") is not None:
            wall[key].append(float(r["elapsed_ms"]))
            outcomes[key][str(r.get("outcome"))] += 1
            completed.add(r.get("corr_id"))
        elif kind == "started":
            started[key].add(r.get("corr_id"))
    table = {}
    for key in set(proc) | set(wall):
        p, s, w = proc.get(key, []), spawn.get(key, []), wall.get(key, [])
        table[key] = {
            "n": len(p),
            "process_ms": {"p50": _pct(p, .5), "p95": _pct(p, .95), "p99": _pct(p, .99), "max": round(max(p), 1) if p else None},
            "over_200ms": sum(x > FIX_BAR_MS for x in p),
            "over_500ms": sum(x > PROCESS_BAR_MS for x in p),
            "process_ms_total": round(sum(p), 1),
            "spawns": {"p95": _pct(s, .95), "max": max(s) if s else None, "total": sum(s)},
            "wall_ms_secondary": {"p95": _pct(w, .95), "max": round(max(w), 1) if w else None},
            "wall_over_door_deadline": sum(x > DOOR_HOOK_DEADLINE_MS for x in w),
            "non_ok_outcomes": {k: v for k, v in outcomes.get(key, {}).items() if k != "ok"},
            "vanished": len(started.get(key, set()) - completed),
        }
    return table


def _slow_legs(rows: list[dict]) -> list[dict]:
    agg: dict[str, list] = {}
    for r in rows:
        if r.get("measurement_scope") != "per_op_handler" or not r.get("legs"):
            continue
        if float(r.get("process_ms") or 0) <= FIX_BAR_MS:
            continue
        for leg in r["legs"]:
            a = agg.setdefault(f"{r.get('op')} :: {leg.get('name')}", [0, 0.0, 0.0])
            ms = float(leg.get("ms") or 0)
            a[0] += 1
            a[1] += ms
            a[2] = max(a[2], ms)
    out = [{"leg": k, "in_slow_calls": v[0], "thread_ms_total": round(v[1]), "thread_ms_max": round(v[2])} for k, v in agg.items()]
    return sorted(out, key=lambda d: -d["thread_ms_total"])[:15]


def _occupancy(rows: list[dict], t0: float, t1: float) -> dict:
    """Busy-worker histogram over warm_server started->complete intervals, by worker pid."""
    starts = {}
    ends = {}
    for r in rows:
        if r.get("route") != "warm_server":
            continue
        if r.get("kind") == "started":
            starts[r.get("corr_id")] = (float(r["t_start"]), r.get("pid"), r.get("op"))
        elif r.get("kind") == "complete" and r.get("elapsed_ms") is not None:
            ends[r.get("corr_id")] = float(r["elapsed_ms"]) / 1000.0
    iv = [(s, s + ends[c], pid, op) for c, (s, pid, op) in starts.items() if c in ends]
    if not iv:
        return {"intervals": 0}
    events = []
    for s, e, pid, _op in iv:
        events.append((s, 1, pid))
        events.append((e, -1, pid))
    events.sort()
    busy = collections.Counter()
    hist = collections.Counter()
    last = max(t0, events[0][0])
    peak = 0
    for t, d, pid in events:
        n = sum(1 for v in busy.values() if v > 0)
        if t > last:
            hist[n] += min(t, t1) - last
        last = max(last, t)
        busy[pid] += d
        peak = max(peak, sum(1 for v in busy.values() if v > 0))
    span = max(1e-9, sum(hist.values()))
    return {
        "intervals": len(iv),
        "worker_pids_seen": len({pid for _s, _e, pid, _o in iv}),
        "peak_busy_workers": peak,
        "share_of_window_busy_workers": {str(k): round(v / span, 4) for k, v in sorted(hist.items())},
        "seconds_at_or_over_heavy_cap": round(sum(v for k, v in hist.items() if k >= GATE_HEAVY_SLOTS), 1),
        "seconds_gate_full": round(sum(v for k, v in hist.items() if k >= GATE_TOTAL_SLOTS), 1),
        "note": "busy = distinct warm_server pids with an open started->complete interval; pids from all sinks read",
    }


def _stalls(runtime_base: Path | None, cutoff: float) -> dict:
    if runtime_base is None:
        local = os.environ.get("LOCALAPPDATA")
        runtime_base = Path(local) if local else Path.home() / ".cache"
    warm = runtime_base / "coordinator" / "warm"
    out = {"runtime": str(warm), "dumps": 0, "by_leg": {}, "by_worker": {}, "hang_restart_lines": 0}
    if not warm.is_dir():
        return out
    legs, workers = collections.Counter(), collections.Counter()
    for svc in warm.iterdir():
        sd = svc / "stall-dumps"
        if sd.is_dir():
            for f in sd.iterdir():
                try:
                    if f.stat().st_mtime < cutoff:
                        continue
                    head = f.read_text(encoding="utf-8", errors="replace")[:4000]
                except OSError:
                    continue
                out["dumps"] += 1
                workers[f.name.split("-")[1] if "-" in f.name else "?"] += 1
                for line in head.splitlines():
                    if line.startswith("leg:"):
                        try:
                            legs[json.loads(line[4:].strip()).get("leg", "?")] += 1
                        except ValueError:
                            legs["?"] += 1
                        break
        hr = svc / "hang-restarts.log"
        if hr.is_file():
            try:
                if hr.stat().st_mtime >= cutoff:
                    out["hang_restart_lines"] += sum(1 for _ in hr.open(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    out["by_leg"] = dict(legs.most_common(10))
    out["by_worker"] = dict(workers)
    return out


def _probe(n: int, interval: float, engine_root: Path | None) -> dict:
    if engine_root is not None:
        sys.path.insert(0, str(engine_root))
    try:
        from coordinator_core.warm import client  # noqa: PLC0415 -- optional, engine checkout only
    except Exception as exc:  # noqa: BLE001
        return {"error": f"engine not importable: {type(exc).__name__}: {exc}"}
    # Scoped: a bare setdefault leaks the origin into the caller (pytest, importers).
    _origin_key = "COORDINATOR_INVOCATION_ORIGIN"
    _origin_prior = os.environ.get(_origin_key)
    os.environ.setdefault(_origin_key, "benchmark")
    try:
        return _probe_loop(n, interval, client)
    finally:
        if _origin_prior is None:
            os.environ.pop(_origin_key, None)
        else:
            os.environ[_origin_key] = _origin_prior


def _probe_loop(n: int, interval: float, client) -> dict:
    samples = []
    for i in range(n):
        msg = {"jsonrpc": "2.0", "id": i + 1, "method": "session.whoami_live", "params": {}}
        s0, p0, w0 = _spawns, time.process_time(), time.perf_counter()
        t_epoch = time.time()
        try:
            resp = client.try_warm_dispatch(msg, read_deadline_secs=DOOR_HOOK_DEADLINE_MS / 1000.0, hook_path=True)
        except Exception as exc:  # noqa: BLE001 -- documented never-raises; record if it does
            resp = {"error": {"message": repr(exc)}}
        rec = {
            "t": round(t_epoch, 3),
            "client_process_ms": round((time.process_time() - p0) * 1000, 2),
            "client_spawns": _spawns - s0,
            "wall_ms_secondary": round((time.perf_counter() - w0) * 1000, 1),
            "hit": resp is not None,
        }
        if isinstance(resp, dict) and resp.get("error"):
            err = resp["error"]
            rec["error"] = {k: err.get(k) for k in ("code", "message") if isinstance(err, dict)}
            data = err.get("data") if isinstance(err, dict) else None
            if isinstance(data, dict):
                rec["error"].update({k: data[k] for k in ("pool_size", "outstanding") if k in data})
        samples.append(rec)
        if i + 1 < n:
            time.sleep(interval)
    cp = [s["client_process_ms"] for s in samples]
    wl = [s["wall_ms_secondary"] for s in samples]
    return {
        "n": n,
        "hits": sum(s["hit"] for s in samples),
        "errors": [s["error"] for s in samples if "error" in s],
        "client_process_ms": {"p50": _pct(cp, .5), "max": max(cp) if cp else None},
        "client_spawns_total": sum(s["client_spawns"] for s in samples),
        "wall_ms_secondary": {"p50": _pct(wl, .5), "max": max(wl) if wl else None},
        "first_t": samples[0]["t"] if samples else None,
        "last_t": samples[-1]["t"] if samples else None,
    }


def _host() -> dict:
    """Host facts without a spawn: platform.platform() shells out on macOS."""
    if sys.platform == "win32":
        v = sys.getwindowsversion()
        release = f"{v.major}.{v.minor}.{v.build}"
    else:
        release = os.uname().release
    return {"os": sys.platform, "release": release, "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "node": platform.node()}


def _session_counts(rows: list[dict]) -> tuple[int, int]:
    """(distinct sids, rows lacking a sid)."""
    sessions = {r.get("sid") for r in rows if r.get("sid")}
    return len(sessions), sum(1 for r in rows if not r.get("sid"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repos", nargs="*", default=[], help="repo roots whose op-latency sinks to read (default: cwd)")
    ap.add_argument("--scan-root", help="read every immediate child repo of this directory too")
    ap.add_argument("--minutes", type=float, default=60.0, help="window of live traffic to read")
    ap.add_argument("--tail-mb", type=float, default=16.0, help="bytes read from the end of each sink")
    ap.add_argument("--probe", type=int, default=0, help="serial warm whoami dials (0 = passive only)")
    ap.add_argument("--probe-interval", type=float, default=2.0)
    ap.add_argument("--engine-root", help="engine checkout to import for --probe (the dir holding coordinator_core)")
    ap.add_argument("--runtime-base", help="override warm runtime base (default %%LOCALAPPDATA%% or ~/.cache)")
    ap.add_argument("--top", type=int, default=25, help="ops shown, hook-class first then by process total")
    ap.add_argument("--check", action="store_true", help="exit 1 if any hook-class op broke the 500ms process bar")
    a = ap.parse_args(argv)

    sys.addaudithook(_audit)
    p_start = time.process_time()
    now = time.time()
    cutoff = now - a.minutes * 60.0
    repos = [Path(r) for r in a.repos] or [Path.cwd()]
    sinks = _sinks(repos, Path(a.scan_root) if a.scan_root else None)
    rows: list[dict] = []
    for s in sinks:
        rows += _read_rows(s, cutoff, int(a.tail_mb * 1024 * 1024))

    table = _op_table(rows)
    hook_keys = sorted((k for k in table if k.startswith(HOOK_PREFIXES)), key=lambda k: -table[k]["process_ms_total"])
    other = sorted((k for k in table if not k.startswith(HOOK_PREFIXES)), key=lambda k: -table[k]["process_ms_total"])
    shown = (hook_keys + other)[: a.top]
    probe = _probe(a.probe, a.probe_interval, Path(a.engine_root) if a.engine_root else None) if a.probe > 0 else None
    distinct_sessions, rows_without_sid = _session_counts(rows)

    report = {
        "schema": "saturation-measure/1",
        "host": _host(),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "window_minutes": a.minutes,
        "sinks_read": [str(s) for s in sinks],
        "rows_read": len(rows),
        "distinct_sessions_in_rows": distinct_sessions,
        "rows_without_sid": rows_without_sid,
        "bar_ms": int(PROCESS_BAR_MS),
        "bars_ms": {"process": PROCESS_BAR_MS, "fix": FIX_BAR_MS, "door_hook_deadline_wall": DOOR_HOOK_DEADLINE_MS},
        "ops": {k: table[k] for k in shown},
        "hook_ops_over_500ms_process": {k: table[k]["over_500ms"] for k in hook_keys if table[k]["over_500ms"]},
        "hook_door_deadline_misses": {k: table[k]["wall_over_door_deadline"] for k in hook_keys if table[k]["wall_over_door_deadline"]},
        "slow_hook_legs": _slow_legs(rows),
        "occupancy": _occupancy(rows, cutoff, now),
        "stalls": _stalls(Path(a.runtime_base) if a.runtime_base else None, cutoff),
        "whoami_probe": probe,
    }
    report["self"] = {"process_ms": round((time.process_time() - p_start) * 1000, 1), "spawns": _spawns}

    print("== saturation-measure: paste everything between the markers ==")
    print("-----BEGIN SATURATION REPORT-----")
    print(json.dumps(report, indent=1, sort_keys=False))
    print("-----END SATURATION REPORT-----")
    if not sinks:
        print("no op-latency sink found: pass --repos <repo root> or --scan-root <dir of repos>", file=sys.stderr)
    return 1 if (a.check and report["hook_ops_over_500ms_process"]) else 0


if __name__ == "__main__":
    sys.exit(main())
