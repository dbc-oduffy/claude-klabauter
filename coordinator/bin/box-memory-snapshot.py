#!/usr/bin/env python3
"""box-memory-snapshot.py [--top N] [--admit-peak GIB] [--local] -- one-screen box memory snapshot (Windows, macOS and Linux).

Fleet-wide by default: after this box's section, one section per far Unimatrix box (live Available,
commit, VRAM, sessions, jobs, plus maxima); an unreachable box keeps its last-known maxima with a
last-seen time and a `power it on?` line, never dropped. With `--admit-peak` each far box also gets
its own verdict from its live reading (no reservation netted; UNKNOWN when unreachable); the exit code
is this box's. `--json` keeps the single-box fields at top level and adds `fleet` and `power_on`.
`--local` is today's single-box output. Admission flags (`--command-key`, `--register-pid`,
`--release-pid`, `--record-peak`, `--run`, `--reservation`, ...) stay this box's own and never fan out.

On Linux available is MemAvailable, commit limit is physical + swap capacity, the cache field is `Cached`,
standby and nonpaged pool print `unavailable`, and a `Memory PSI` line gives the some/full avg10 of
/proc/pressure/memory. Admission is the same as on macOS.

On macOS available is the kernel figure, commit limit is physical + swap capacity, nonpaged pool
and cache fields print `unavailable`, and top consumers rank by `ri_phys_footprint` (resident +
compressed + swapped), which can far exceed `ps` RSS. Prints Available, commit used vs limit, system cache working set, standby, nonpaged pool and the
top N consumers by private commit. A counter that fails to read prints `unavailable` for that
field alone. `--admit-peak GIB` adds one line, `ADMIT ...` (exit 0) or `HOLD ...` naming the
failed clause (exit 4); the figure is net of `reserved=`, the sum over live START pids without an
END in admission.jsonl of max(0, declared_peak_mb - tree RSS). Need is peak + min(0.25*peak, 8 GiB);
`--fleet` drops the cushion (need is peak) for a fleet already sized with its reserve S.
`--command-key K` with `--record-peak GIB` keeps K's largest seen peak (used when `--admit-peak` is omitted, except on a record or release call);
`--register-pid P` on ADMIT reserves P's headroom, `--release-pid P` frees it. `--reservation` prints
each registered running job's headroom and the total in GiB (an exited job is absent: it adds 0). `--json` emits the same
fields plus `gpu`: one in-process `gpu_sample.sample()` reading as free/total VRAM per device and in sum,
and `jobs` holding only pids with >= 1 GiB VRAM, or `{"unavailable": reason}` where no GPU is readable.
Other hosts: exit 6.

Exit codes: ADMIT 0, HOLD 4, ERROR 5 (admission could not be decided: unreadable status or an uncaught
exception). Under `--json` with `--admit-peak` (or a `--command-key` peak) every such path prints one
`{"admit": {"verdict": "ERROR", "error": "<reason>"}}` object on stdout and the reason on stderr; a
reservation-selection refusal prints `verdict` HOLD with `error`, exit 4.
Usage errors under `--json` with an admit flag also print the ERROR object and keep argparse's exit 2
(SystemExit propagates); abbreviated flags are refused.
Not typed: launcher/interpreter/exe-shim failures before this script runs (this script never starts).

With ``--run R``, only the actual invoking session's live grant for R can receive
credit. ``--session-id`` confirms that identity; it cannot replace it. The explicit
``--parent-pool-run P`` selector instead uses native registry lineage and an active,
birth-stamped parent START. Child allocations share its registered peak atomically.
Selected ``--register-pid`` and ``--release-pid`` use the actual session/run pair;
release does not require a still-current grant. Without selectors accounting is unchanged.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from types import ModuleType

PROG = "box-memory-snapshot"
EXIT_ERROR = 5
_LIB = Path(__file__).resolve().parent / "lib"
_UNAVAILABLE = "unavailable"
_READ_FIELDS = ("available_gib", "commit_used_gib", "system_cache_ws_gib", "nonpaged_pool_gib", "top_consumers")
_FAILED_READ_NOTE = (
    "a failed read is not evidence of a kernel leak; memory reads fail under commit exhaustion. "
    "Read process_counts: a runaway python/conhost count is the leading signal."
)


def _place_engine_on_path() -> None:
    """Put the engine root on `sys.path` ahead of a lazy `coordinator_core` import; fail-open."""
    import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
    import cc_invoke

    cc_invoke.ensure_engine_on_path(__file__)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _LIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # the host's @dataclass classes resolve annotations through it
    spec.loader.exec_module(module)
    return module


def _load_cache_host():
    """Instantiate the promoted Win32FileCacheHost; its sibling import needs lib on sys.path."""
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    import file_cache_win32

    return file_cache_win32.Win32FileCacheHost()


def _gib(value: int) -> float:
    return round(value / 1024**3, 2)


def _try(fn: Callable):
    try:
        return fn()
    except Exception:
        return None


def _top_consumers_gov(gov, top: int) -> list:
    foot = gov.footprints()
    cmds = {p.pid: p.cmd for p in gov.list_procs()}
    ranked = sorted(foot.items(), key=lambda kv: kv[1], reverse=True)[:top]
    return [
        {"pid": pid, "private_commit_mb": round(mb, 1), "cmd": cmds.get(pid, "")[:100]}
        for pid, mb in ranked
    ]


def _top_consumers_table(proc_table_fn: Callable, cmdline_fn: Callable, top: int) -> list:
    """Rank the one process-table read by private bytes; read a cmdline for the top N rows only."""
    ranked = sorted(proc_table_fn(), key=lambda r: r.private_bytes, reverse=True)[: max(top, 0)]
    return [
        {
            "pid": r.pid,
            "private_commit_mb": round(r.private_bytes / 1024**2, 1),
            "cmd": (cmdline_fn(r.pid) or "")[:100],
        }
        for r in ranked
    ]


_COUNTED = ("python", "conhost")


def _psutil_image_names() -> list:
    import psutil

    return [p.info["name"] or "" for p in psutil.process_iter(["name"])]


def _image_stem(name: str) -> str:
    low = name.lower()
    low = low[:-4] if low.endswith(".exe") else low
    return "python" if low.startswith("python") else low


def _process_counts(table_fn: Optional[Callable], names_fn: Callable) -> Optional[dict]:
    """Per-image counts (python, conhost): the process table first, then psutil; None if both fail.

    In-process reads only: a spawn is the read most likely to fail under commit exhaustion.
    """
    names = None
    if table_fn is not None:
        rows = _try(table_fn)
        if rows is not None:
            names = [getattr(r, "image", "") or "" for r in rows]
            if not any(names):  # a table without image names is no count
                names = None
    if names is None:
        names = _try(names_fn)
    if names is None:
        return None
    stems = [_image_stem(n) for n in names]
    return {k: stems.count(k) for k in _COUNTED}


_GPU_JOB_MIN_MIB = 1024


def _gpu_object(reading) -> dict:
    """Fold one `gpu_sample.sample()` reading into the snapshot's `gpu` object; pure.

    `{"unavailable": reason}` for a `gpu_unavailable` or malformed reading. Otherwise per-device free
    and total VRAM (free is None where used is unreadable, as on unified memory), their sums, and only
    the jobs holding >= 1 GiB (desktop clients appear as compute apps). Per-pid VRAM the driver
    withholds is already absent from `vram_by_pid`; it is never reported as 0.
    """
    gpus = reading.get("gpus") if isinstance(reading, dict) else None
    if not isinstance(gpus, list) or not gpus:
        reason = reading.get("gpu_unavailable") if isinstance(reading, dict) else None
        return {"unavailable": reason or "gpu reading malformed"}
    devices = []
    for g in gpus:
        total, used = g.get("vram_total_mib"), g.get("vram_used_mib")
        free = max(total - used, 0) if total is not None and used is not None else None
        devices.append({
            "index": g.get("index"),
            "name": g.get("name"),
            "vram_total_mib": total,
            "vram_free_mib": free,
            "unified": bool(g.get("unified")),
        })

    def _sum(key: str) -> Optional[int]:
        vals = [d[key] for d in devices]
        return sum(vals) if vals and all(v is not None for v in vals) else None

    by_pid = reading.get("vram_by_pid") or {}
    jobs = sorted(
        ({"pid": int(pid), "vram_mib": mib} for pid, mib in by_pid.items() if mib >= _GPU_JOB_MIN_MIB),
        key=lambda j: j["vram_mib"],
        reverse=True,
    )
    return {
        "vram_total_mib": _sum("vram_total_mib"),
        "vram_free_mib": _sum("vram_free_mib"),
        "devices": devices,
        "jobs": jobs,
    }


def _read_gpu() -> dict:
    """The `gpu` object from one in-process NVML / unified-memory reading; never raises."""
    try:
        _place_engine_on_path()
        from coordinator_core.group_em import gpu_sample

        return _gpu_object(gpu_sample.sample())
    except Exception as exc:
        return {"unavailable": f"gpu-read-error:{type(exc).__name__}"}


def _render_gpu(gpu: dict) -> list:
    if "unavailable" in gpu:
        return [f"GPU:              {_UNAVAILABLE}: {gpu['unavailable']}"]

    def _mib(v: Optional[int]) -> str:
        return _UNAVAILABLE if v is None else f"{round(v / 1024, 2)} GiB"

    lines = [f"GPU VRAM:         {_mib(gpu['vram_free_mib'])} free / {_mib(gpu['vram_total_mib'])}"]
    jobs = gpu["jobs"]
    lines.append(
        "GPU jobs >=1 GiB: " + ("none" if not jobs else "  ".join(f"{j['pid']}={_mib(j['vram_mib'])}" for j in jobs))
    )
    return lines


def build_snapshot(
    status,
    nonpaged_fn,
    cache_host_fn,
    consumers_fn: Callable,
    top: int,
    counts_fn: Optional[Callable] = None,
    gpu_fn: Optional[Callable] = None,
) -> dict:
    cache = _try(lambda: cache_host_fn().read_memory())
    nonpaged = _try(nonpaged_fn)
    consumers = _try(lambda: consumers_fn(top))
    counts = _try(counts_fn) if counts_fn else None
    snap = {
        "available_gib": _gib(status.avail_phys) if status else _UNAVAILABLE,
        "commit_used_gib": _gib(status.commit_used) if status else _UNAVAILABLE,
        "commit_limit_gib": _gib(status.commit_limit) if status else _UNAVAILABLE,
        "system_cache_ws_gib": _gib(cache.cache_bytes) if cache else _UNAVAILABLE,
        "standby_gib": _gib(cache.standby_bytes) if cache and cache.standby_bytes is not None else _UNAVAILABLE,
        "nonpaged_pool_gib": _gib(nonpaged) if nonpaged is not None else _UNAVAILABLE,
        "top_consumers": consumers if consumers is not None else _UNAVAILABLE,
        "process_counts": counts if counts is not None else _UNAVAILABLE,
        "gpu": (gpu_fn or _read_gpu)(),
    }
    failed = [k for k in _READ_FIELDS if snap[k] == _UNAVAILABLE]
    if failed:
        snap["note"] = _FAILED_READ_NOTE
    return snap


def _render(snap: dict) -> str:
    lines = [
        f"Available:        {snap['available_gib']} GiB",
        f"Commit:           {snap['commit_used_gib']} / {snap['commit_limit_gib']} GiB",
        f"System cache WS:  {snap['system_cache_ws_gib']} GiB",
        f"Standby:          {snap['standby_gib']} GiB",
        f"Nonpaged pool:    {snap['nonpaged_pool_gib']} GiB",
    ]
    pc = snap["process_counts"]
    if pc == _UNAVAILABLE:
        lines.append(f"Process counts:   {_UNAVAILABLE}")
    else:
        lines.append("Process counts:   " + "  ".join(f"{k}={v}" for k, v in pc.items()))
    psi = snap.get("memory_psi")
    if psi:
        lines.append("Memory PSI:       " + "  ".join(f"{k}={v}" for k, v in psi.items()))
    lines.extend(_render_gpu(snap["gpu"]))
    if "note" in snap:
        lines.append(f"Note:             {snap['note']}")
    top = snap["top_consumers"]
    if top == _UNAVAILABLE:
        lines.append(f"Top consumers:    {_UNAVAILABLE}")
    else:
        lines.append("Top consumers by private commit:")
        for row in top:
            lines.append(f"  {row['pid']:>7}  {row['private_commit_mb']:>9.1f} MB  {row['cmd']}")
    return "\n".join(lines)


def _render_reservation(res: dict) -> str:
    lines = ["Reservation (GO-admitted running jobs, max(0, declared peak - tree RSS)):"]
    for j in res["jobs"]:
        lines.append(f"  {'grant' if j['pid'] is None else j['pid']:>7}  {j['reserved_gib']:>7.2f} GiB  {j['run']}")
    lines.append(f"Reservation total: {res['total_gib']} GiB")
    return "\n".join(lines)


_LOCAL_ONLY_ARGS = (
    "register_pid", "release_pid", "record_peak", "command_key", "run", "session_id",
    "parent_pool_run", "reservation", "admission_file", "commit_limit_gib",
)
_MB = 1024**2
_DOWN_LABEL = {"timed_out": "UNREACHABLE (timed out)", "unreachable": "UNREACHABLE"}


def _fleet_wanted(args) -> bool:
    """Fleet-wide unless `--local`, or a flag that reads or writes this box's own admission state."""
    return not args.local and all(getattr(args, name) in (None, False) for name in _LOCAL_ONLY_ARGS)


def _no_local_sample() -> dict:
    raise RuntimeError("this box is read by the local snapshot")


def _fleet_fetch() -> dict:
    """Far boxes' live rows (`fleet_live.gather`, one invoke each, in parallel, per-box timeout) plus
    this box's alias; never raises: `{"error": reason}` on failure."""
    try:
        _place_engine_on_path()
        from coordinator_core.group_em import box_registry, fleet_live

        local = box_registry.local_box_id()
        rows = fleet_live.gather(local_sample=_no_local_sample, local_id=local)
        return {"local_alias": fleet_live.box_alias(None), "rows": [r for r in rows if r.get("box") != local]}
    except Exception as exc:  # noqa: BLE001 -- a fleet read never sinks this box's snapshot
        return {"error": f"{type(exc).__name__}: {exc}"}


def _start_fleet(fetch: Callable) -> tuple:
    """Run `fetch` on a daemon thread so the far reads overlap this box's own; `(thread, result_box)`."""
    box: list = []

    def _run() -> None:
        try:
            box.append(fetch())
        except Exception as exc:  # noqa: BLE001
            box.append({"error": f"{type(exc).__name__}: {exc}"})

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, box


def _mb_gib(mb) -> str:
    return _UNAVAILABLE if not isinstance(mb, (int, float)) else f"{round(mb / 1024, 2)}"


def _ago(then, now: float) -> str:
    if not isinstance(then, (int, float)):
        return "never"
    s = max(0, int(now - then))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def _far_admit(row: dict, peak_gib: float, cushion_pct: float, host) -> dict:
    """This far box's verdict on `peak_gib` from its live reading (no reservation netted: a far box's
    admission is its own). An UNREACHABLE box or a missing reading is `UNKNOWN`, never ADMIT."""
    if row.get("state") != "ok":
        return {"verdict": "UNKNOWN", "peak_gib": peak_gib, "reason": str(row.get("reason") or row.get("state"))}
    keys = ("ram_free_mb", "ram_total_mb", "commit_used_mb", "commit_limit_mb")
    if any(not isinstance(row.get(k), (int, float)) for k in keys):
        return {"verdict": "UNKNOWN", "peak_gib": peak_gib, "reason": "reading missing"}
    status = host.MemoryStatus(
        int(row["ram_free_mb"] * _MB), int(row["ram_total_mb"] * _MB),
        int(row["commit_used_mb"] * _MB), int(row["commit_limit_mb"] * _MB),
    )
    verdict = host.admit(int(peak_gib * host.GIB), status, cushion_pct=cushion_pct)
    return {
        "verdict": "ADMIT" if verdict.admit else "HOLD",
        "peak_gib": peak_gib,
        "need_avail_gib": _gib(int(verdict.need_avail)),
        "avail_ok": verdict.avail_ok,
        "commit_ok": verdict.commit_ok,
        "line": _admit_line(verdict, peak_gib, status, status.commit_limit),
    }


def _maxima_line(row: dict) -> str:
    gpus = [g for g in (row.get("gpus") or []) if isinstance(g, dict)]
    gpu = ", ".join(f"{g.get('name') or '?'} {_mb_gib(g.get('vram_total_mib'))} GiB" for g in gpus) or "no gpu"
    cores = row.get("cores")
    return (f"{cores if cores is not None else _UNAVAILABLE} cores, {_mb_gib(row.get('ram_total_mb'))} GiB RAM, "
            f"commit limit {_mb_gib(row.get('commit_limit_mb'))} GiB, {gpu}")


def _render_far(row: dict, admit: Optional[dict], now: float) -> str:
    """One far box's section; a box that did not answer keeps its last-known maxima and last-seen time."""
    name = row.get("alias") or row.get("box")
    head = f"== {name} ({row.get('os') or '?'})"
    if row.get("state") != "ok":
        label = _DOWN_LABEL.get(str(row.get("state")), str(row.get("state")).upper())
        lines = [f"{head} {label} ({row.get('reason') or row.get('state')}) ==",
                 f"Maxima (last known): {_maxima_line(row) if row.get('maxima_source') else _UNAVAILABLE}",
                 f"Last seen:        {_ago(row.get('last_seen'), now)} ago"]
    else:
        vram = row.get("vram_used_mib")
        jobs = row.get("jobs")
        lines = [
            f"{head} ==",
            f"Available:        {_mb_gib(row.get('ram_free_mb'))} GiB",
            f"Commit:           {_mb_gib(row.get('commit_used_mb'))} / {_mb_gib(row.get('commit_limit_mb'))} GiB",
            f"GPU VRAM used:    {_mb_gib(vram)} GiB",
            f"Sessions/jobs:    {row.get('sessions') if row.get('sessions') is not None else _UNAVAILABLE}"
            f" / {len(jobs) if isinstance(jobs, list) else _UNAVAILABLE}",
            f"Maxima:           {_maxima_line(row)}",
            f"Sampled:          {_ago(row.get('last_seen'), now)} ago",
        ]
    if admit is not None:
        lines.append(admit["line"] + " reserved=n/a" if "line" in admit
                     else f"UNKNOWN peak={admit.get('peak_gib', '?')}GiB ({admit['reason']})")
    return "\n".join(lines)


def _render_fleet(fleet: dict, admits: dict, now: float) -> list:
    """Far-box sections plus one `power it on?` line per UNREACHABLE box; `[]` when the fleet read failed
    (a note line instead)."""
    if "error" in fleet:
        return [f"== fleet == unavailable ({fleet['error']})"]
    lines = []
    for row in fleet["rows"]:
        lines.append(_render_far(row, admits.get(row["box"]), now))
    for row in fleet["rows"]:
        if row.get("state") in _DOWN_LABEL:
            lines.append(f"power it on? {row.get('alias') or row.get('box')} is UNREACHABLE "
                         f"(last seen {_ago(row.get('last_seen'), now)} ago)")
    return lines


def _admit_line(verdict, peak_gib: float, status, limit: int) -> str:
    if verdict.admit:
        return f"ADMIT peak={peak_gib}GiB need_avail={_gib(int(verdict.need_avail))}GiB"
    failed = []
    if not verdict.avail_ok:
        failed.append(
            f"available: need {_gib(int(verdict.need_avail))}GiB > avail {_gib(status.avail_phys)}GiB"
        )
    if not verdict.commit_ok:
        failed.append(
            f"commit: used {_gib(status.commit_used)}GiB + peak {peak_gib}GiB > limit {_gib(limit)}GiB"
        )
    return f"HOLD peak={peak_gib}GiB failed {'; '.join(failed)}"


def _print_admit_error(reason: str, peak_gib: Optional[float] = None, verdict: str = "ERROR") -> None:
    admit = {"verdict": verdict, "error": reason}
    if peak_gib is not None:
        admit["peak_gib"] = peak_gib
    print(json.dumps({"admit": admit}))
    _JSON_EMITTED[0] = True


_JSON_EMITTED = [False]  # one typed JSON object per run: a late exception never prints a second
_TYPED_FLAGS = ("--admit-peak", "--command-key")


def _wants_typed_admit(argv: list) -> bool:
    """True for `--json` plus an admit flag, or a prefix of one (argparse would reject the abbreviation)."""
    def _admit_flag(a: str) -> bool:
        head = a.split("=", 1)[0]
        return len(head) >= 4 and head.startswith("--") and any(f.startswith(head) for f in _TYPED_FLAGS)

    return "--json" in argv and any(_admit_flag(a) for a in argv)


def main(argv: Optional[list] = None, **kwargs) -> int:
    """Run the snapshot; under `--json` with `--admit-peak`/`--command-key`, an uncaught exception prints one
    `{"admit": {"verdict": "ERROR", "error": ...}}` object and exits `EXIT_ERROR`."""
    raw = list(sys.argv[1:] if argv is None else argv)
    _JSON_EMITTED[0] = False
    try:
        return _main(argv, **kwargs)
    except SystemExit as exc:  # argparse usage errors exit 2 after printing usage to stderr
        if exc.code in (0, None) or not _wants_typed_admit(raw):
            raise
        if not _JSON_EMITTED[0]:
            _print_admit_error(f"usage error (exit {exc.code}); see stderr")
        raise
    except Exception as exc:
        if not _wants_typed_admit(raw):
            raise
        reason = f"{type(exc).__name__}: {exc}"
        print(f"{PROG}: {reason}", file=sys.stderr)
        if not _JSON_EMITTED[0]:
            _print_admit_error(reason)
        return EXIT_ERROR


def _main(
    argv: Optional[list] = None,
    *,
    platform: Optional[str] = None,
    status_fn: Optional[Callable] = None,
    nonpaged_fn: Optional[Callable] = None,
    cache_host_fn: Optional[Callable] = None,
    governor=None,
    rss_fn: Optional[Callable] = None,
    alive_fn: Optional[Callable] = None,
    create_time_fn: Optional[Callable] = None,
    image_fn: Optional[Callable] = None,
    proc_table_fn: Optional[Callable] = None,
    cmdline_fn: Optional[Callable] = None,
    names_fn: Optional[Callable] = None,
    gpu_fn: Optional[Callable] = None,
    fleet_fn: Optional[Callable] = None,
    owner_pid_fn: Optional[Callable] = None,
) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description="One-screen box memory snapshot.", allow_abbrev=False)
    parser.add_argument("--top", type=int, default=10, help="top N consumers (default 10)")
    parser.add_argument("--admit-peak", type=float, metavar="GIB", help="check admission of a peak; the default "
                        "mode fits a single heavy job and adds a cushion of min(--cushion-pct of peak, 8 GiB); "
                        "an ADMIT reserves the peak for the caller's parent pid until it exits "
                        "(release: --command-key admit-peak --release-pid P)")
    parser.add_argument("--fleet", action="store_true", help="admit with no cushion: available >= peak and "
                        "commit + peak <= limit only. For a fleet already sized with its reserve S")
    parser.add_argument("--cushion-pct", type=float, default=25.0, help="available cushion percent")
    parser.add_argument("--commit-limit-gib", type=float, help="override the commit limit")
    parser.add_argument("--admission-file", help="admission.jsonl path (default: settings-home channel)")
    parser.add_argument("--local", action="store_true", help="this box only (default: every Unimatrix box; "
                        "admission flags and --reservation are always this box's own)")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--command-key", help="name of the command; keys its recorded peak")
    parser.add_argument("--run", help="credit only this invoking session's live Group EM slot for this run")
    parser.add_argument("--session-id", help="confirm the invoking identity for --run; cannot select a different session")
    parser.add_argument("--parent-pool-run", help="explicitly draw from the native parent session's active registered pool for this run")
    parser.add_argument("--record-peak", type=float, metavar="GIB", help="record this command's seen peak (max kept)")
    parser.add_argument("--register-pid", type=int, help="on ADMIT, reserve this pid's headroom until release or exit")
    parser.add_argument("--release-pid", type=int, help="free the reservation registered for this pid")
    parser.add_argument("--reservation", action="store_true", help="print each GO-admitted running job's "
                        "max(0, declared peak - tree RSS) and their total in GiB; exited jobs add 0")
    args = parser.parse_args(argv)
    if args.session_id is not None and not args.run:
        parser.error("--session-id requires --run")
    if args.parent_pool_run and not args.run:
        parser.error("--parent-pool-run requires the child's --run")
    if args.run and args.admit_peak is None and not args.command_key:
        parser.error("--run requires --admit-peak or a recorded --command-key peak")
    if args.fleet:  # a fleet's width formula already took its reserve S out; a cushion pads it twice
        args.cushion_pct = 0.0
    fleet_wanted = _fleet_wanted(args)

    host = _load("box_memory_host")
    adm_path = args.admission_file or host.admission_file()
    if args.command_key and args.release_pid is not None:
        if args.run:
            _place_engine_on_path()
            from coordinator_core.group_em import box_registry
            try:
                sid = box_registry.admission_session_id(args.session_id)
            except ValueError as exc:
                print(f"{PROG}: reservation selection refused: {exc}", file=sys.stderr)
                return 4
            host.release_command(adm_path, args.command_key, args.release_pid,
                                 reservation_session_id=sid, reservation_run=args.run)
        else:
            host.release_command(adm_path, args.command_key, args.release_pid)
    if args.command_key and args.record_peak is not None:
        host.record_peak(adm_path, args.command_key, args.record_peak)
    if args.admit_peak is None and args.command_key and args.record_peak is None and args.release_pid is None:
        args.admit_peak = host.read_recorded_peak(adm_path, args.command_key)
    if args.run and args.admit_peak is None and args.release_pid is None:
        parser.error("--run requires a known recorded peak or explicit --admit-peak")
    plat = platform if platform is not None else sys.platform
    if plat not in ("win32", "darwin") and not plat.startswith("linux"):
        return host.unsupported(PROG, plat, "reads Windows, macOS or Linux memory counters")
    if governor is None and proc_table_fn is None and names_fn is None:
        missing = host.require_psutil(PROG)
        if missing is not None:
            return missing

    fleet_thread, fleet_box = _start_fleet(fleet_fn or _fleet_fetch) if fleet_wanted else (None, [])
    status_fn = status_fn or host.read_memory_status
    if plat == "darwin":
        # No nonpaged pool or file-cache counters on macOS: those fields print `unavailable`.
        nonpaged_fn = nonpaged_fn or (lambda: None)
        cache_host_fn = cache_host_fn or (lambda: SimpleNamespace(read_memory=lambda: None))
    elif plat.startswith("linux"):
        # No nonpaged pool or standby counter on Linux; the file cache is meminfo's Cached.
        nonpaged_fn = nonpaged_fn or (lambda: None)
        cache_host_fn = cache_host_fn or (lambda: SimpleNamespace(read_memory=lambda: (lambda c: SimpleNamespace(
            cache_bytes=c, standby_bytes=None) if c is not None else None)(host.linux_cached_bytes())))
    else:
        nonpaged_fn = nonpaged_fn or host.nonpaged_pool_bytes
        cache_host_fn = cache_host_fn or _load_cache_host
    table_fn = None
    if plat == "win32":
        table_fn = proc_table_fn or host.proc_table
        cmd_fn = cmdline_fn or host.proc_cmdline
        consumers_fn = lambda top: _top_consumers_table(table_fn, cmd_fn, top)
    else:
        gov_fn = (lambda: governor) if governor is not None else host.load_governor
        consumers_fn = lambda top: _top_consumers_gov(gov_fn(), top)

    status_error: list = []

    def _read_status():
        try:
            return status_fn()
        except Exception as exc:
            status_error.append(str(exc))
            raise

    status = _try(_read_status)
    counts_fn = lambda: _process_counts(table_fn, names_fn or _psutil_image_names)
    snap = build_snapshot(status, nonpaged_fn, cache_host_fn, consumers_fn, args.top, counts_fn, gpu_fn)
    if plat.startswith("linux"):
        snap["memory_psi"] = _try(host.read_psi_memory)
    code = 0
    admit_line = None
    reserved_kw = {
        **({"rss_fn": rss_fn} if rss_fn else {}),
        **({"alive_fn": alive_fn} if alive_fn else {}),
        **({"create_time_fn": create_time_fn} if create_time_fn else {}),
        **({"image_fn": image_fn} if image_fn else {}),
    }
    if args.reservation:
        jobs = host.top_holders(adm_path, limit=sys.maxsize, **reserved_kw)
        snap["reservation"] = {
            "jobs": [
                {"session_id": j["session_id"], "run": j["run"], "pid": j["pid"], "reserved_gib": _gib(j["bytes"])}
                for j in jobs
            ],
            "total_gib": _gib(sum(j["bytes"] for j in jobs)),
        }
    if args.admit_peak is not None:
        if status is None:
            detail = f": {status_error[0]}" if status_error and status_error[0] else ""
            print(f"{PROG}: cannot read memory status for admission{detail}", file=sys.stderr)
            if args.json:
                _print_admit_error(f"cannot read memory status for admission{detail}", args.admit_peak)
            return EXIT_ERROR
        peak = int(args.admit_peak * host.GIB)
        limit = int(args.commit_limit_gib * host.GIB) if args.commit_limit_gib is not None else status.commit_limit
        if args.run:
            _place_engine_on_path()
            from coordinator_core.group_em import box_registry, slot_grant
            try:
                sid = box_registry.admission_session_id(args.session_id)
                selector = (lambda: slot_grant.parent_pool_admission_selector(
                    sid, args.run, args.parent_pool_run, args.admit_peak * 1024
                )) if args.parent_pool_run else (lambda: slot_grant.own_admission_selector(sid, args.run, args.admit_peak * 1024))
                verdict, reserved, selection = host.admit_selected_reservation(
                    adm_path, args.admit_peak, status, selector,
                    key=args.command_key if args.register_pid is not None else None, pid=args.register_pid,
                    cushion_pct=args.cushion_pct, commit_limit=limit, **reserved_kw,
                )
            except (OSError, ValueError) as exc:
                print(f"{PROG}: reservation selection refused: {exc}", file=sys.stderr)
                if args.json:
                    _print_admit_error(f"reservation selection refused: {exc}", args.admit_peak, verdict="HOLD")
                return 4
            snap["reservation_selection"] = {**selection, "credited_gib": _gib(selection["credited_bytes"])}
        elif args.command_key and args.register_pid is not None:
            verdict, reserved = host.admit_and_register(
                adm_path, args.command_key, args.register_pid, args.admit_peak, status,
                cushion_pct=args.cushion_pct, commit_limit=limit, **reserved_kw,
            )
        else:
            owner = (owner_pid_fn or os.getppid)()
            if owner and owner > 0:
                verdict, reserved = host.admit_and_register(
                    adm_path, args.command_key or "admit-peak", owner, args.admit_peak, status,
                    cushion_pct=args.cushion_pct, commit_limit=limit, credit_own=True, **reserved_kw,
                )
            else:
                reserved = host.reserved_bytes(adm_path, **reserved_kw)
                adjusted = dataclasses.replace(
                    status,
                    avail_phys=max(0, status.avail_phys - reserved),
                    commit_used=status.commit_used + reserved,
                )
                verdict = host.admit(peak, adjusted, cushion_pct=args.cushion_pct, commit_limit=limit)
        # Display only: the verdict above already netted `reserved`.
        status = dataclasses.replace(
            status,
            avail_phys=max(0, status.avail_phys - reserved),
            commit_used=status.commit_used + reserved,
        )
        admit_line = _admit_line(verdict, args.admit_peak, status, limit) + f" reserved={_gib(reserved)}GiB"
        snap["admit"] = {
            "reserved_gib": _gib(reserved),
            "verdict": "ADMIT" if verdict.admit else "HOLD",
            "peak_gib": args.admit_peak,
            "need_avail_gib": _gib(int(verdict.need_avail)),
            "avail_ok": verdict.avail_ok,
            "commit_ok": verdict.commit_ok,
        }
        code = 0 if verdict.admit else 4

    fleet: dict = {}
    admits: dict = {}
    if fleet_thread is not None:
        fleet_thread.join()
        fleet = fleet_box[0] if fleet_box else {"error": "no reply"}
        if args.admit_peak is not None and "rows" in fleet:
            admits = {r["box"]: _far_admit(r, args.admit_peak, args.cushion_pct, host) for r in fleet["rows"]}
    now = time.time()
    if args.json:
        if fleet:
            snap["fleet"] = (
                [{**r, "admit": admits[r["box"]]} if r["box"] in admits else r for r in fleet["rows"]]
                if "rows" in fleet else {"unavailable": fleet["error"]}
            )
            if "rows" in fleet:
                snap["power_on"] = [r["box"] for r in fleet["rows"] if r.get("state") in _DOWN_LABEL]
        print(json.dumps(snap))
        _JSON_EMITTED[0] = True
    else:
        if fleet:
            print(f"== {fleet.get('local_alias', 'this box')} (this box) ==")
        print(_render(snap))
        if "reservation" in snap:
            print(_render_reservation(snap["reservation"]))
        if admit_line:
            print(admit_line)
        if fleet:
            for line in _render_fleet(fleet, admits, now):
                print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
