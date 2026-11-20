"""Warm write stall probe: does a caller wait on a blocked filesystem write, and where do the stalls come from.

Five measurements, each on a fixture under `--root` (default: a managed dir under the repo's `scratch/`),
never on the live record homes:

1. admits: N concurrent writers call `run_token.admits` (its heartbeat stamp is the write) against a run
   record whose `last_activity_at` is stale, so every call owes one stamp;
2. claim lookup: N concurrent writers call `claim_index.lookup` with no index built, so each owes an
   index build (`_begin_index`: rename-aside, `makedirs`, token file);
3. unbounded writes: `land_leg_record.write` and `turn_intent._atomic_write` called directly, plain and
   with `SlowFs` armed; a `within_bar` of False under the shim is the N1 gap still open for that frame;
4. raw latency: `mkstemp` + write + `os.replace` in a fresh dir vs a dir seeded with the live `state/warp`
   entry count (the directory-size split);
5. dump classification: today's warm-engine stall dumps, each N1 frame tallied by whether the thread it
   blocks is a caller (op handler) or a worker (any other thread), plus MsMpEng CPU at stall time.

Phases 1 and 2 run twice: plain, and with `SlowFs` armed (`mkstemp`, `os.replace`, `os.makedirs` under the
fixture block for `--stall-secs`). Per phase: process ms per op (process CPU over the phase / ops), spawn
count (`subprocess.Popen` constructions), and the caller's wait per call (p50/p95/max). Wall clock alone
carries no verdict (DR-344); the caller wait is the quantity a stalled write takes from an op's budget.

    python -m coordinator_core.benchmarks.warm_write_stall_probe [--writers 8] [--rounds 3]
        [--stall-secs 10] [--entries auto|N] [--samples 40] [--root DIR] [--dumps DIR] [--day YYYYMMDD|all] [--json]

TRAP: the shim patches Python-level calls, so it models a kernel-blocked create on every host but not a
filter driver below them; a fixed tree must return to its caller inside `CALLER_WAIT_BAR_MS`; each phase's `within_bar` is p95 <= that bar.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator, Optional

from coordinator_core.benchmarks.fs_create_stall_probe import _pct, live_svc_dir

DEFAULT_WRITERS = 8
DEFAULT_ROUNDS = 3
DEFAULT_STALL_SECS = 10.0
DEFAULT_SAMPLES = 40
FALLBACK_ENTRIES = 5186
SESSION_DIRS = 24
CALLER_WAIT_BAR_MS = 200.0
PROCESS_BAR_MS = 500.0
_OFFPATH_JOIN_SECS = 5.0
_OFFPATH_THREADS = ("run-activity-heartbeat", "claim-index-build")
_OLD_STAMP = "2020-01-01T00:00:00Z"

#: Frames of the N1 list: label, thread-frame path substring (forward slashes), function, and the
#: pattern its `legs-ms` row carries (the dump's per-leg elapsed ms, which outlives the thread snapshot).
N1_FRAMES = (
    ("pathlib.stat", "pathlib", "stat", r"_local\.py::stat"),
    ("pathlib.open", "pathlib", "open", r"_local\.py::open"),
    ("pathlib.read_bytes", "pathlib", "read_bytes", r"::read_bytes"),
    ("tempfile._mkstemp_inner", "tempfile.py", "_mkstemp_inner", r"tempfile\.py::_mkstemp_inner"),
    ("ntpath.realpath", "ntpath.py", "realpath", r"ntpath>?::realpath"),
    ("atomic_replace.atomic_write_bytes", "coordinator_core/atomic_replace.py", "atomic_write_bytes", r"atomic_replace\.py::atomic_write_bytes"),
    ("turn_intent._atomic_write", "turn_intent.py", "_atomic_write", r"turn_intent\.py::_atomic_write"),
    ("land_leg_record.write", "land_leg_record.py", "write", r"land_leg_record\.py::write"),
    ("landing_integrity._store_verdict", "landing_integrity.py", "_store_verdict", r"landing_integrity\.py::_store_verdict"),
    ("commit.checkin_blob_sha", "commit.py", "checkin_blob_sha", r"commit\.py::checkin_blob_sha"),
    ("log_rotation.rotate_if_needed", "log_rotation.py", "rotate_if_needed", r"log_rotation\.py::rotate_if_needed"),
    ("locked_write._assert_anchor_outside_target", "locked_write.py", "_assert_anchor_outside_target", r"locked_write\.py::_assert_anchor_outside_target"),
    ("claim_index._begin_index", "claim_index.py", "_begin_index", r"claim_index\.py::_begin_index"),
)
_LEG_ROW = re.compile(r"\[(\d+), (\d+), \"([^\"]*)\"\]")
#: A `legs-ms` row at or above this many ms counts as a stalled leg.
LEG_STALL_MS = 1000
#: A thread running an op handler is the caller; every other thread is a worker.
_CALLER_FUNCS = frozenset({"_run_handler_recording", "_run_handler_absorbing_base"})
_FS_INNERMOST = frozenset({
    "_mkstemp_inner", "mkstemp", "open", "read_bytes", "read_text", "stat", "replace", "unlink", "realpath",
    "makedirs", "mkdir", "_path_stat", "_read", "write", "rename", "scandir", "_isdir", "lstat", "_readlink",
})
_FRAME_ROW = re.compile(r'^\s*File "([^"]+)", line (\d+) in (\S+)')


def _under(path: Any, root: str) -> bool:
    try:
        target = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        return target == root or target.startswith(root + os.sep)
    except (TypeError, ValueError):
        return False


class SlowFs:
    """Blocks `tempfile.mkstemp`, `os.replace` and `os.makedirs` for `stall_secs` when their target is under
    `root`, on whichever thread calls them. `release()` frees every blocked call at once. Patches Python-level
    names only; nothing outside `root` is touched."""

    def __init__(self, root: Path, stall_secs: float) -> None:
        self._root = os.path.normcase(os.path.abspath(str(root)))
        self._stall = stall_secs
        self._release = threading.Event()
        self._lock = threading.Lock()
        self.hits = {"mkstemp": 0, "replace": 0, "makedirs": 0}
        self._saved: dict = {}

    def release(self) -> None:
        self._release.set()

    def _block(self, op: str) -> None:
        with self._lock:
            self.hits[op] += 1
        self._release.wait(self._stall)

    def __enter__(self) -> "SlowFs":
        orig_mkstemp, orig_replace, orig_makedirs = tempfile.mkstemp, os.replace, os.makedirs
        self._saved = {"mkstemp": orig_mkstemp, "replace": orig_replace, "makedirs": orig_makedirs}

        def mkstemp(suffix=None, prefix=None, dir=None, text=False) -> object:  # noqa: ANN001
            if dir is not None and _under(dir, self._root):
                self._block("mkstemp")
            return orig_mkstemp(suffix, prefix, dir, text)

        def replace(src, dst, *a, **kw) -> object:  # noqa: ANN001, ANN002, ANN003
            if _under(dst, self._root):
                self._block("replace")
            return orig_replace(src, dst, *a, **kw)

        def makedirs(name, *a, **kw) -> object:  # noqa: ANN001, ANN002, ANN003
            if _under(name, self._root):
                self._block("makedirs")
            return orig_makedirs(name, *a, **kw)

        setattr(tempfile, "mkstemp", mkstemp)
        setattr(os, "replace", replace)
        setattr(os, "makedirs", makedirs)
        return self

    def __exit__(self, *exc: object) -> None:
        self._release.set()
        setattr(tempfile, "mkstemp", self._saved["mkstemp"])
        setattr(os, "replace", self._saved["replace"])
        setattr(os, "makedirs", self._saved["makedirs"])


@contextlib.contextmanager
def _count_spawns() -> Generator[list, None, None]:
    """Counts `subprocess.Popen` constructions (every `run`/`check_output` goes through one)."""
    count = [0]
    orig = subprocess.Popen.__init__

    def init(self, *a, **kw) -> None:  # noqa: ANN001, ANN002, ANN003
        count[0] += 1
        orig(self, *a, **kw)

    subprocess.Popen.__init__ = init  # type: ignore[method-assign]
    try:
        yield count
    finally:
        subprocess.Popen.__init__ = orig  # type: ignore[method-assign]


@contextlib.contextmanager
def _settings_home(path: Path) -> Generator[None, None, None]:
    saved = os.environ.get("COORDINATOR_SETTINGS_HOME")
    os.environ["COORDINATOR_SETTINGS_HOME"] = str(path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("COORDINATOR_SETTINGS_HOME", None)
        else:
            os.environ["COORDINATOR_SETTINGS_HOME"] = saved


class Fixture:
    """`repo/` (a `.git` dir, `state/warp`), `settings/` and `sessions/` under one root."""

    def __init__(self, root: Path, entries: int) -> None:
        self.root = root
        self.repo = root / "repo"
        self.settings = root / "settings"
        self.sessions = root / "sessions"
        (self.repo / ".git").mkdir(parents=True, exist_ok=True)
        self.settings.mkdir(parents=True, exist_ok=True)
        self.sessions.mkdir(parents=True, exist_ok=True)
        from coordinator_core.session import record_homes

        self.warp_home = Path(record_homes.home_dir(str(self.repo), "warp"))
        self.warp_home.mkdir(parents=True, exist_ok=True)
        for i in range(entries):
            (self.warp_home / f"entry-{i:05d}").mkdir(exist_ok=True)
        for i in range(SESSION_DIRS):
            (self.sessions / f"sid-{i:03d}").mkdir(exist_ok=True)
        self.entries = entries

    def index_root(self) -> Path:
        from coordinator_core.session import touch_record

        return Path(touch_record.claim_index_dir(self.sessions)).parent


def _stage_path(fx: Fixture, run_id: str) -> Path:
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    return fx.warp_home / run_id / RUN_STAGE_FILE


def _write_stale_stage(fx: Fixture, run_id: str) -> None:
    path = _stage_path(fx, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"stage": "staged", "phase": "executing", "last_activity_at": _OLD_STAMP, "session_id": "s"}),
        encoding="utf-8",
    )


def _stamp_advanced(fx: Fixture, run_id: str) -> bool:
    try:
        return json.loads(_stage_path(fx, run_id).read_text(encoding="utf-8")).get("last_activity_at") != _OLD_STAMP
    except (OSError, ValueError):
        return False


def _reset_module_state() -> None:
    from coordinator_core.ops.dispatch_emit import ask_contract
    from coordinator_core.session import claim_index

    with contextlib.suppress(AttributeError):
        ask_contract._DEFERRED_HEARTBEATS.clear()
    for name in ("_BUILDS", "_INDEX_NAMES_CACHE"):
        getattr(claim_index, name, {}).clear()


def _join_offpath() -> None:
    deadline = time.monotonic() + _OFFPATH_JOIN_SECS
    for t in threading.enumerate():
        if t.name in _OFFPATH_THREADS:
            t.join(max(0.0, deadline - time.monotonic()))


def _drive(
    writers: int, rounds: int, setup: Callable[[int], None], call: Callable[[int], object],
) -> dict:
    """N writers, `rounds` barrier-aligned rounds; `setup` is unmeasured, `call` is the caller's wait."""
    waits: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(writers)

    def writer(i: int) -> None:
        for _ in range(rounds):
            try:
                setup(i)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"setup:{exc!r}")
            barrier.wait()
            t0 = time.perf_counter()
            try:
                call(i)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"call:{exc!r}")
            with lock:
                waits.append((time.perf_counter() - t0) * 1000.0)
            barrier.wait()

    cpu0 = time.process_time()
    with _count_spawns() as spawns:
        threads = [threading.Thread(target=writer, args=(i,), daemon=True) for i in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    ops = max(1, len(waits))
    return {
        "ops": len(waits),
        "within_bar": _pct(waits, 0.95) <= CALLER_WAIT_BAR_MS,
        "process_ms_per_op": round((time.process_time() - cpu0) * 1000.0 / ops, 3),
        "spawns": spawns[0],
        "caller_wait_ms": {
            "p50": round(_pct(waits, 0.5), 3), "p95": round(_pct(waits, 0.95), 3), "max": round(max(waits, default=0.0), 3),
        },
        "errors": errors[:3],
    }


def measure_admits(fx: Fixture, writers: int, rounds: int, stall_secs: Optional[float]) -> dict:
    """`run_token.admits` under N writers; `stall_secs` arms `SlowFs`, None leaves the filesystem alone."""
    from coordinator_core.ops.dispatch_emit import run_token

    script = "scratch/warp/probe-script.js"
    ids = [f"warp-probe-{i}" for i in range(writers)]
    tokens = [run_token.mint(fx.repo, rid, script) for rid in ids]
    _reset_module_state()
    shim = SlowFs(fx.root, stall_secs) if stall_secs is not None else None
    with shim if shim is not None else contextlib.nullcontext():
        phase = _drive(
            writers, rounds, lambda i: _write_stale_stage(fx, ids[i]),
            lambda i: run_token.admits(fx.repo, ids[i], tokens[i], script),
        )
        if shim is not None:
            shim.release()
        _join_offpath()
        if shim is not None:
            phase["shim_hits"] = dict(shim.hits)
    phase["write_landed"] = all(_stamp_advanced(fx, rid) for rid in ids)
    return phase


def measure_claim_lookup(fx: Fixture, writers: int, rounds: int, stall_secs: Optional[float]) -> dict:
    """`claim_index.lookup` with no index built, under N writers (each owes an index build)."""
    from coordinator_core.session import claim_index, touch_record

    def reset(_i: int) -> None:
        if _i == 0:
            shutil.rmtree(fx.index_root(), ignore_errors=True)
            _reset_module_state()

    _reset_module_state()
    shim = SlowFs(fx.root, stall_secs) if stall_secs is not None else None
    with shim if shim is not None else contextlib.nullcontext():
        phase = _drive(
            writers, rounds, reset, lambda i: claim_index.lookup([f"probe/path-{i}.txt"], str(fx.sessions)),
        )
        if shim is not None:
            shim.release()
        _join_offpath()
        if shim is not None:
            phase["shim_hits"] = dict(shim.hits)
    phase["write_landed"] = (Path(touch_record.claim_index_dir(fx.sessions)) / touch_record.CLAIM_INDEX_COMPLETE).exists()
    return phase


def measure_unbounded_writes(fx: Fixture, writers: int, rounds: int, stall_secs: Optional[float]) -> dict:
    """`land_leg_record.write` and `turn_intent._atomic_write` called directly under N writers: the N1 frames
    with no `run_bounded` around them, so a blocked `os.replace` is the caller's wait."""
    from coordinator_core.hooks.support import turn_intent
    from coordinator_core.ops.dispatch_emit import ask_contract, land_leg_record

    turn_dir = fx.root / "turn-intent"
    turn_dir.mkdir(parents=True, exist_ok=True)
    ids = [f"warp-probe-land-{i}" for i in range(writers)]
    out: dict = {}
    for label, call in (
        ("land_leg_record.write", lambda i: land_leg_record.write(
            fx.repo, ids[i], outcome=land_leg_record.OUTCOME_HALT, leg="probe", bound_by=None, legs=[])),
        ("turn_intent._atomic_write", lambda i: turn_intent._atomic_write(str(turn_dir), f"w{i}.json", "{}")),
    ):
        shim = SlowFs(fx.root, stall_secs) if stall_secs is not None else None
        with shim if shim is not None else contextlib.nullcontext():
            phase = _drive(writers, rounds, lambda i: None, call)
            if shim is not None:
                shim.release()
                phase["shim_hits"] = dict(shim.hits)
        out[label] = phase
    out["land_leg_record.write"]["write_landed"] = all(
        (ask_contract.record_dir(fx.repo, rid) / land_leg_record.FILE_NAME).exists() for rid in ids
    )
    out["turn_intent._atomic_write"]["write_landed"] = all((turn_dir / f"w{i}.json").exists() for i in range(writers))
    return out


def _timed_create(work: Path, i: int) -> tuple[float, float]:
    c0, w0 = time.thread_time(), time.perf_counter()
    fd, tmp = tempfile.mkstemp(prefix=".atomic-write.", dir=str(work))
    try:
        os.write(fd, b"x" * 4096)
    finally:
        os.close(fd)
    os.replace(tmp, work / f"target-{i % 8}")
    return (time.perf_counter() - w0) * 1000.0, (time.thread_time() - c0) * 1000.0


def measure_raw_latency(fresh: Path, large: Path, samples: int) -> dict:
    """`mkstemp` + write + `os.replace` in an empty dir and in `large` (already seeded)."""
    out: dict = {}
    for label, work in (("fresh_dir", fresh), ("large_dir", large)):
        work.mkdir(parents=True, exist_ok=True)
        rows = [_timed_create(work, i) for i in range(samples)]
        wall, cpu = [r[0] for r in rows], [r[1] for r in rows]
        out[label] = {
            "entries": sum(1 for _ in os.scandir(work)),
            "wall_ms": {"p50": round(_pct(wall, 0.5), 3), "p95": round(_pct(wall, 0.95), 3), "max": round(max(wall), 3)},
            "cpu_ms": {"p50": round(_pct(cpu, 0.5), 3), "p95": round(_pct(cpu, 0.95), 3), "max": round(max(cpu), 3)},
        }
    f95, l95 = out["fresh_dir"]["wall_ms"]["p95"], out["large_dir"]["wall_ms"]["p95"]
    out["large_over_fresh_p95"] = round(l95 / f95, 2) if f95 > 0 else None
    return out


def parse_threads(text: str) -> list[list[tuple[str, str]]]:
    """Each thread block of a dump's `--- threads ---` section as `[(path, func)]`, innermost first."""
    body = text.partition("--- threads ---")[2]
    threads: list[list[tuple[str, str]]] = []
    for block in re.split(r"\n\s*\n", body):
        frames = []
        for line in block.splitlines():
            m = _FRAME_ROW.match(line)
            if m:
                frames.append((m.group(1).replace("\\", "/"), m.group(3)))
        if frames:
            threads.append(frames)
    return threads


def classify_dumps(dump_dir: Path, day: str = "") -> dict:
    """Tally each N1 frame over a dump dir's thread stacks: caller vs worker thread, and how often the
    thread's innermost frame is a filesystem primitive. `day` (YYYYMMDD) keeps dumps of that UTC date;
    "" keeps every dump."""
    files = sorted(p for p in dump_dir.glob("*.txt") if p.name.startswith(day)) if dump_dir.is_dir() else []
    tally = {
        label: {"caller": 0, "worker": 0, "innermost_fs": 0, "legs_ge_1s": 0, "leg_max_ms": 0}
        for label in (row[0] for row in N1_FRAMES)
    }
    msmpeng: list[float] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^box-msmpeng-cpu-pct: ([\d.]+)", text, re.M)
        if m:
            msmpeng.append(float(m.group(1)))
        head = text.partition("--- threads ---")[0]
        for line in head.splitlines():
            if line.startswith("legs-ms:"):
                for _start, elapsed, leg in _LEG_ROW.findall(line):
                    for label, _p, _f, leg_pat in N1_FRAMES:
                        if re.search(leg_pat, leg):
                            tally[label]["leg_max_ms"] = max(tally[label]["leg_max_ms"], int(elapsed))
                            tally[label]["legs_ge_1s"] += int(int(elapsed) >= LEG_STALL_MS)
        for frames in parse_threads(text):
            kind = "caller" if any(func in _CALLER_FUNCS for _p, func in frames) else "worker"
            innermost_fs = frames[0][1] in _FS_INNERMOST
            for label, path_part, func, _leg_pat in N1_FRAMES:
                if any(func == f and path_part in p for p, f in frames):
                    tally[label][kind] += 1
                    tally[label]["innermost_fs"] += int(innermost_fs)
    return {
        "dump_dir": str(dump_dir),
        "day": day or "all",
        "dumps": len(files),
        "frames": tally,
        "msmpeng_cpu_pct": {"n": len(msmpeng), "p50": round(_pct(msmpeng, 0.5), 1), "max": max(msmpeng, default=0.0)},
    }


def _live_entries(repo: Path) -> int:
    from coordinator_core.session import record_homes

    try:
        return sum(1 for _ in os.scandir(record_homes.home_dir(str(repo), "warp")))
    except OSError:
        return FALLBACK_ENTRIES


def measure(
    writers: int = DEFAULT_WRITERS,
    rounds: int = DEFAULT_ROUNDS,
    stall_secs: float = DEFAULT_STALL_SECS,
    entries: Optional[int] = None,
    samples: int = DEFAULT_SAMPLES,
    root: Optional[Path] = None,
    dumps: Optional[Path] = None,
    day: Optional[str] = None,
) -> dict:
    """The structured result `render` prints. Fixture dirs live under `root` and are removed on exit."""
    repo = Path(__file__).resolve().parents[2]
    n_entries = entries if entries is not None else _live_entries(repo)
    base = root if root is not None else repo / "scratch"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="warm-write-stall-probe-", dir=str(base)))
    try:
        with _settings_home(work / "settings"):
            fx = Fixture(work / "fx", n_entries)
            result: dict = {
                "writers": writers, "rounds": rounds, "stall_secs": stall_secs, "warp_entries": n_entries,
                "bars": {"caller_wait_ms": CALLER_WAIT_BAR_MS, "process_ms_per_op": PROCESS_BAR_MS},
                "admits": {"plain": measure_admits(fx, writers, rounds, None)},
            }
            result["admits"]["slow_fs"] = measure_admits(fx, writers, 1, stall_secs)
            result["claim_lookup"] = {"plain": measure_claim_lookup(fx, writers, rounds, None)}
            result["claim_lookup"]["slow_fs"] = measure_claim_lookup(fx, writers, 1, stall_secs)
            result["unbounded_writes"] = {
                "plain": measure_unbounded_writes(fx, writers, rounds, None),
                "slow_fs": measure_unbounded_writes(fx, writers, 1, stall_secs),
            }
        large = work / "large"
        large.mkdir()
        for i in range(n_entries):
            (large / f"entry-{i:05d}").mkdir()
        result["raw_latency"] = measure_raw_latency(work / "fresh", large, samples)
        day_key = day if day is not None else datetime.now(timezone.utc).strftime("%Y%m%d")
        result["dump_classification"] = classify_dumps(
            dumps if dumps is not None else live_svc_dir() / "stall-dumps", "" if day_key == "all" else day_key,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def _phase_line(label: str, p: dict) -> str:
    w = p["caller_wait_ms"]
    return (
        f"  {label}: ops={p['ops']} process-ms/op={p['process_ms_per_op']} spawns={p['spawns']} "
        f"caller-wait-ms p50={w['p50']} p95={w['p95']} max={w['max']} within-bar={p['within_bar']} write-landed={p['write_landed']}"
        + (f" shim-hits={p['shim_hits']}" if "shim_hits" in p else "")
        + (f" errors={p['errors']}" if p["errors"] else "")
    )


def render(result: dict) -> str:
    lines = [
        f"== warm write stall probe: writers={result['writers']} rounds={result['rounds']} "
        f"stall={result['stall_secs']}s warp-entries={result['warp_entries']} =="
    ]
    for op in ("admits", "claim_lookup"):
        lines.append(f"{op}:")
        for variant, phase in result[op].items():
            lines.append(_phase_line(variant, phase))
    lines.append("unbounded writes (land_leg_record, turn_intent):")
    for variant, ops in result["unbounded_writes"].items():
        for label, phase in ops.items():
            lines.append(_phase_line(f"{variant} {label}", phase))
    raw = result["raw_latency"]
    lines.append("raw mkstemp+replace latency:")
    for label in ("fresh_dir", "large_dir"):
        r = raw[label]
        lines.append(f"  {label} entries={r['entries']} wall p50={r['wall_ms']['p50']} p95={r['wall_ms']['p95']} max={r['wall_ms']['max']} ms | cpu p95={r['cpu_ms']['p95']} ms")
    lines.append(f"  large/fresh wall p95 ratio={raw['large_over_fresh_p95']}")
    d = result["dump_classification"]
    lines.append(f"stall dumps {d['dump_dir']} day={d['day']} dumps={d['dumps']} msmpeng-cpu-pct n={d['msmpeng_cpu_pct']['n']} p50={d['msmpeng_cpu_pct']['p50']} max={d['msmpeng_cpu_pct']['max']}")
    for label, t in d["frames"].items():
        lines.append(
            f"  {label}: caller={t['caller']} worker={t['worker']} innermost-fs={t['innermost_fs']} "
            f"legs>=1s={t['legs_ge_1s']} leg-max-ms={t['leg_max_ms']}"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--writers", type=int, default=DEFAULT_WRITERS)
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--stall-secs", type=float, default=DEFAULT_STALL_SECS)
    ap.add_argument("--entries", default="auto", help="state/warp-sized dir entry count (auto = the live count)")
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--root", type=Path, default=None, help="fixture parent dir (default <repo>/scratch)")
    ap.add_argument("--dumps", type=Path, default=None, help="stall-dumps dir (default the live engine's)")
    ap.add_argument("--day", default=None, help="UTC YYYYMMDD of dumps to classify, or 'all' (default today)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = measure(
        args.writers, args.rounds, args.stall_secs, None if args.entries == "auto" else int(args.entries),
        args.samples, args.root, args.dumps, args.day,
    )
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
