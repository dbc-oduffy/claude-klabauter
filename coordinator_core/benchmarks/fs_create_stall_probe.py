"""Open/create stall probe: where do 10-20 s single `mkstemp`/`open`/`read_text` calls come from.

Three read-only-to-the-box measurements (no Defender or filesystem configuration is changed):

1. dump survey -- every `<svc_dir>/stall-dumps/*.txt` (svc_dir resolved by `breadcrumb.svc_dir`) is parsed for
   `legs-ms` rows whose innermost frame is a filesystem primitive, tabulated by primitive, and each dump's
   `box-msmpeng-cpu-pct` is compared between fs-stall dumps and the rest;
2. create/open timing -- `mkstemp`+write+close+replace+unlink, and first-touch `read_text` of engine-clone `.py`
   files, per location (repo tree, system temp, the engine svc dir, any `--path`), each reported as wall ms and
   thread CPU ms with MsMpEng's CPU delta over the location's run;
3. Defender state (`Get-MpComputerStatus`, read-only; exclusions need admin and are reported as unreadable) and
   open-handle holders of each probe dir (psutil, time-boxed).

Wall ms is an I/O-block signal here (CPU ~0 while wall is large); the DR-344 bar is read off CPU ms.
Run: `python -m coordinator_core.benchmarks.fs_create_stall_probe [--creates N] [--reads N] [--path DIR ...]`.
Probe files live in a managed dir under each location and are removed on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.temp_layout import mkdtemp_in
from coordinator_core.win_portability import no_console_creationflags

DEFAULT_CREATES = 150
DEFAULT_READS = 150
DEFAULT_SOAK_S = 60.0
#: A soak op slower than this is recorded as a stall event.
SLOW_MS = 100.0
#: An fs leg at or above this many ms in a dump counts as a stall.
STALL_MS = 1000
HANDLE_SCAN_BUDGET_S = 3.0
DEFENDER_IMAGE = "msmpeng.exe"
#: Innermost-frame substrings that mark a leg as a filesystem call.
FS_PRIMITIVES = (
    "_mkstemp_inner",
    "mkstemp",
    "::open",
    "read_text",
    "read_bytes",
    "get_data",
    "_path_stat",
    "::stat",
    "::replace",
    "::unlink",
    "::_read",
    "::write",
)
_LEG_ROW = re.compile(r"\[(\d+), (\d+), \"([^\"]*)\"\]")


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _summ(wall: list[float], cpu: list[float]) -> str:
    return (
        f"n={len(wall)} wall p50={_pct(wall, .5):.2f} p95={_pct(wall, .95):.2f} max={max(wall, default=0):.2f} ms | "
        f"cpu p50={_pct(cpu, .5):.3f} p95={_pct(cpu, .95):.3f} max={max(cpu, default=0):.3f} ms"
    )


def _defender_cpu_s() -> Optional[float]:
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() == DEFENDER_IMAGE:
            try:
                t = proc.cpu_times()
                return t.user + t.system
            except Exception:  # noqa: BLE001 - access denied is a normal answer
                return None
    return None


def _timed(fn: Callable[[], object]) -> tuple[float, float]:
    """Wall ms and thread CPU ms of one call."""
    c0, w0 = time.thread_time(), time.perf_counter()
    fn()
    return (time.perf_counter() - w0) * 1000.0, (time.thread_time() - c0) * 1000.0


def survey_dumps(dump_dir: Path) -> dict:
    """Tabulate `legs-ms` fs legs and MsMpEng CPU across a stall-dump directory."""
    by_primitive: dict[str, list[int]] = {}
    fs_msmpeng: list[float] = []
    other_msmpeng: list[float] = []
    files = sorted(dump_dir.glob("*.txt")) if dump_dir.is_dir() else []
    fs_dumps = 0
    for path in files:
        try:
            head = path.read_text(encoding="utf-8", errors="replace").partition("--- threads ---")[0]
        except OSError:
            continue
        legs = []
        for line in head.splitlines():
            if line.startswith("legs-ms:"):
                legs = [(int(a), int(b), c) for a, b, c in _LEG_ROW.findall(line)]
        stalled = []
        for _start, elapsed, leg in legs:
            tail = leg.rsplit(" > ", 1)[-1]
            if elapsed >= STALL_MS and any(p in tail for p in FS_PRIMITIVES):
                stalled.append((elapsed, tail))
        m = re.search(r"^box-msmpeng-cpu-pct: ([\d.]+)", head, re.M)
        pct = float(m.group(1)) if m else None
        if stalled:
            fs_dumps += 1
            for elapsed, tail in stalled:
                by_primitive.setdefault(tail, []).append(elapsed)
            if pct is not None:
                fs_msmpeng.append(pct)
        elif pct is not None:
            other_msmpeng.append(pct)
    return {
        "dump_dir": str(dump_dir),
        "dumps": len(files),
        "fs_stall_dumps": fs_dumps,
        "by_primitive": {k: sorted(v) for k, v in by_primitive.items()},
        "msmpeng_fs": fs_msmpeng,
        "msmpeng_other": other_msmpeng,
    }


def probe_creates(base: Path, count: int, name_prefix: str = ".atomic-write.") -> tuple[list[float], list[float]]:
    """mkstemp + write 4 KiB + close + os.replace + unlink, `count` times under `base`."""
    base.mkdir(parents=True, exist_ok=True)
    work = mkdtemp_in("fs-create-probe-", base)
    wall: list[float] = []
    cpu: list[float] = []
    payload = b"x" * 4096
    try:
        for i in range(count):
            target = work / f"target-{i % 8}"

            def one() -> None:
                fd, tmp = tempfile.mkstemp(prefix=name_prefix, dir=str(work))
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                os.replace(tmp, target)

            w, c = _timed(one)
            wall.append(w)
            cpu.append(c)
    finally:
        rmtree_or_raise(work, label="fs-create-probe")
    return wall, cpu


def probe_concurrent_soak(bases: dict[str, Path], seconds: float) -> dict:
    """One paced create loop per location, all at once, for `seconds`.

    The intermittent stall is caught by duration. An event (op over SLOW_MS) landing in every location in the
    same instant is a common-mode (filesystem/box-wide) stall; one landing in a single location is per-volume or
    per-directory. Returns {"events": [(t_s, label, ms)], "ops": {label: n}, "msmpeng_cpu_s": delta}."""
    events: list[tuple[float, str, float]] = []
    ops: dict[str, int] = {label: 0 for label in bases}
    lock = threading.Lock()
    t0 = time.monotonic()
    deadline = t0 + seconds
    d0 = _defender_cpu_s()

    def loop(label: str, base: Path) -> None:
        base.mkdir(parents=True, exist_ok=True)
        work = mkdtemp_in("fs-soak-", base)
        try:
            while time.monotonic() < deadline:
                def one() -> None:
                    fd, tmp = tempfile.mkstemp(prefix=".atomic-write.", dir=str(work))
                    os.write(fd, b"x" * 4096)
                    os.close(fd)
                    os.replace(tmp, work / "target")

                w, _c = _timed(one)
                with lock:
                    ops[label] += 1
                    if w > SLOW_MS:
                        events.append((time.monotonic() - t0, label, w))
                time.sleep(0.02)
        finally:
            rmtree_or_raise(work, label="fs-soak")

    threads = [threading.Thread(target=loop, args=(lbl, b), daemon=True) for lbl, b in bases.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 60)
    d1 = _defender_cpu_s()
    return {
        "events": sorted(events),
        "ops": ops,
        "msmpeng_cpu_s": None if d0 is None or d1 is None else d1 - d0,
    }


def probe_reads(root: Path, count: int) -> tuple[list[float], list[float]]:
    """read_text of up to `count` distinct `.py` files under `root` (first touch for files not yet cached)."""
    wall: list[float] = []
    cpu: list[float] = []
    for path in sorted(root.rglob("*.py"))[:count] if root.is_dir() else []:
        w, c = _timed(lambda p=path: p.read_text(encoding="utf-8", errors="replace"))
        wall.append(w)
        cpu.append(c)
    return wall, cpu


def defender_state() -> dict:
    """Read-only Defender query; exclusions are admin-only and reported as such."""
    if sys.platform != "win32":
        return {"available": False}
    # Exclusion paths are admin-only via Get-MpPreference; the Operational log's event 5007 carries each
    # add/remove, so the live set is replayed oldest-first (a lone Old-value line is a removal).
    script = (
        "$s=Get-MpComputerStatus; $p=Get-MpPreference; $set=@{}; "
        "$ev=Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Windows Defender/Operational';Id=5007} "
        "-MaxEvents 5000 -ErrorAction SilentlyContinue; if($ev){[array]::Reverse($ev)}; "
        "foreach($e in $ev){ foreach($l in ($e.Message -split \"`n\")){ "
        "if($l -match '(Old|New) value: HKLM\\\\SOFTWARE\\\\Microsoft\\\\Windows Defender\\\\Exclusions\\\\Paths\\\\(.+?) = 0x'){ "
        "if($Matches[1] -eq 'New'){$set[$Matches[2]]=1}else{ if(-not ($e.Message -match 'New value: HKLM.*Exclusions\\\\Paths\\\\'+[regex]::Escape($Matches[2]))){$set.Remove($Matches[2])}}}}}; "
        "[pscustomobject]@{rtp=$s.RealTimeProtectionEnabled;onaccess=$s.OnAccessProtectionEnabled;"
        "ioav=$s.IoavProtectionEnabled;cloud_block=$p.CloudBlockLevel;cloud_ext_timeout=$p.CloudExtendedTimeout;"
        "excl=@($p.ExclusionPath);excl_from_events=@($set.Keys)}|ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True, text=True, timeout=20, **no_console_creationflags(),
        )
        data = json.loads(out.stdout.strip() or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": repr(exc)}
    excl = data.get("excl") or []
    data["exclusions_readable"] = not any("administrator" in str(e).lower() for e in excl)
    return data


def handle_holders(dirs: Iterable[Path]) -> list[str]:
    """Processes whose open files or cwd sit under any probe dir (psutil, time-boxed)."""
    try:
        import psutil
    except ImportError:
        return ["psutil unavailable"]
    prefixes = [str(d).lower() for d in dirs]
    found: list[str] = []

    def scan() -> None:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                paths = [f.path for f in proc.open_files()]
                paths.append(proc.cwd())
            except Exception:  # noqa: BLE001
                continue
            for p in paths:
                if any(p.lower().startswith(pre) for pre in prefixes):
                    found.append(f"{proc.info['name']} pid={proc.info['pid']} {p}")
                    break

    t = threading.Thread(target=scan, daemon=True)
    t.start()
    t.join(HANDLE_SCAN_BUDGET_S)
    if t.is_alive():
        found.append(f"scan exceeded {HANDLE_SCAN_BUDGET_S}s budget (partial)")
    return found


def live_svc_dir() -> Path:
    """The svc dir holding the most stall dumps written in the last 72 h: the long-lived engine's, which runs
    from the klabauter publish clone, not necessarily this checkout (`breadcrumb.svc_dir()` is the fallback)."""
    from coordinator_core.warm import breadcrumb

    own = breadcrumb.svc_dir()
    best, best_n = own, -1
    cutoff = time.time() - 72 * 3600
    for cand in own.parent.glob("*/stall-dumps"):
        try:
            n = sum(1 for f in cand.iterdir() if f.stat().st_mtime >= cutoff)
        except OSError:
            continue
        if n > best_n:
            best, best_n = cand.parent, n
    return best


def _locations(extra: list[str]) -> list[tuple[str, Path]]:
    repo = Path(__file__).resolve().parents[2]
    locs = [
        ("repo-tree", repo / "scratch" / "fs-create-stall-probe"),
        ("system-temp", Path(tempfile.gettempdir()) / "fs-create-stall-probe"),
        ("engine-svc-dir", live_svc_dir() / "fs-create-stall-probe"),
    ]
    locs += [(f"path:{p}", Path(p)) for p in extra]
    return locs


def measure(
    creates: int = DEFAULT_CREATES,
    reads: int = DEFAULT_READS,
    extra: Optional[list[str]] = None,
    soak_s: float = DEFAULT_SOAK_S,
) -> dict:
    """The structured result the report is printed from."""
    repo = Path(__file__).resolve().parents[2]
    result: dict = {"locations": {}, "defender": defender_state()}
    svc = live_svc_dir()
    result["dump_survey"] = survey_dumps(svc / "stall-dumps")
    locs = _locations(extra or [])
    for label, base in locs:
        d0 = _defender_cpu_s()
        cw, cc = probe_creates(base, creates)
        d1 = _defender_cpu_s()
        entry = {"create": (cw, cc), "msmpeng_cpu_s": None if d0 is None or d1 is None else d1 - d0}
        if label == "repo-tree":
            entry["create_plain_tmp"] = probe_creates(base, creates, name_prefix="plain-")
        result["locations"][label] = entry
    if soak_s > 0:
        result["soak"] = probe_concurrent_soak(dict(locs), soak_s)
    result["reads"] = {"repo-tree": probe_reads(repo / "coordinator_core", reads)}
    result["holders"] = handle_holders([b for _l, b in locs])
    for _label, base in locs:
        shutil.rmtree(base, ignore_errors=True)
        try:
            base.rmdir()
        except OSError:
            pass
    return result


def render(result: dict) -> str:
    lines = ["== defender (read-only) =="]
    lines.append(json.dumps(result["defender"]))
    s = result["dump_survey"]
    lines.append(f"== dump survey {s['dump_dir']} ==")
    lines.append(f"dumps={s['dumps']} dumps-with-fs-stall(>={STALL_MS}ms)={s['fs_stall_dumps']}")
    for prim, vals in sorted(s["by_primitive"].items(), key=lambda kv: -max(kv[1])):
        lines.append(f"  {prim}: n={len(vals)} max={max(vals)}ms p50={int(statistics.median(vals))}ms")
    lines.append(
        f"msmpeng-cpu-pct fs-stall dumps: n={len(s['msmpeng_fs'])} p50={_pct(s['msmpeng_fs'], .5):.1f} "
        f"max={max(s['msmpeng_fs'], default=0):.1f} | other dumps: n={len(s['msmpeng_other'])} "
        f"p50={_pct(s['msmpeng_other'], .5):.1f} max={max(s['msmpeng_other'], default=0):.1f}"
    )
    lines.append("== create (mkstemp+write+replace) ==")
    for label, e in result["locations"].items():
        lines.append(f"{label}: {_summ(*e['create'])} msmpeng-cpu-s-delta={e['msmpeng_cpu_s']}")
        if "create_plain_tmp" in e:
            lines.append(f"{label} (name 'plain-*'): {_summ(*e['create_plain_tmp'])}")
    if "soak" in result:
        soak = result["soak"]
        lines.append(
            f"== concurrent soak (all locations at once; event = op > {SLOW_MS:.0f} ms) ops={soak['ops']} "
            f"msmpeng-cpu-s-delta={soak['msmpeng_cpu_s']} events={len(soak['events'])} =="
        )
        for t_s, label, ms in soak["events"]:
            lines.append(f"  t={t_s:.2f}s {label} {ms:.0f} ms")
    lines.append("== read_text ==")
    for label, (w, c) in result["reads"].items():
        lines.append(f"{label} coordinator_core/*.py: {_summ(w, c)}")
    lines.append("== handle holders under probe dirs ==")
    lines.extend(result["holders"] or ["none found"])
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--creates", type=int, default=DEFAULT_CREATES)
    ap.add_argument("--reads", type=int, default=DEFAULT_READS)
    ap.add_argument("--path", action="append", default=[], help="extra directory to probe (repeatable)")
    ap.add_argument("--soak-s", type=float, default=DEFAULT_SOAK_S, help="concurrent paced-create soak seconds (0 = skip)")
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    print(render(measure(args.creates, args.reads, args.path, args.soak_s)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
