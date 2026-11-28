"""Dirty-tree falsifier for `dispatch.measured_rev`: process time, spawns and a per-phase table on a
tmp git repo with many dirty tracked paths and many run records under `state/warp`.

The measured call is the production handler (`measured_rev._handler`, with its `call_io_scope` and
`index_read_cache_scope`), run in-process after a tiny warm-up call has imported every lazy module.
Pass `first` is the enforced figure: the first call on the full fixture, uninstrumented, process time
(`time.process_time` plus descendants) and `subprocess.Popen` count. Pass `repeat` is a second call on
the same fixture. Pass `attribution` runs a fresh fixture with each phase wrapped and filesystem calls
counted per phase (`open`, `stat`, `scan` = scandir/listdir; Python-level calls only, so a count is a
floor); its total is higher than `first` by the wrapper cost and is never the enforced figure.
Wall time is recorded as `wall_minus_process_ms` (blocked, scheduled-out and IO time together) and is
never counted as process time.

Phases (exclusive process time; a nested wrapped call is charged to the inner phase only):
  run_landing_paths  drop_unmeasurable  spine  py_parse  checkin  write_object  tree_rewrite
  peer_split  touched_own_split  reach_seeds  reach_paths  foreign_dirt  other (unwrapped remainder)

    python coordinator_core/benchmarks/measured_rev_dirty_tree_budget.py [--dirty N] [--untracked N]
        [--runs N] [--paths N] [--enforce-bar] [--json]

`--enforce-bar` exits 2 when pass `first` is at or over BAR_MS process time, any spawn happened, or the
call did not return a candidate commit. A process spawned by the call would also count against
PROCESS_BAR_MS; spawns > 0 already fails, so that bar is reported, not separately timed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402

BAR_MS = 500.0
PROCESS_BAR_MS = 200.0
DEFAULT_DIRTY = 800
DEFAULT_RUNS = 200
DEFAULT_PATHS = 24
DEFAULT_REPEAT = 8
PKG_COUNT = 40
HALTED_SHARE = 0.7
REACH_FANOUT = 6
PEER_HUNK_EVERY = 3
STAMP_STEP_S = 60
EPOCH = 1_700_000_000
EXIT_OVER_BAR = 2
PHASES = (
    "run_landing_paths", "drop_unmeasurable", "spine", "py_parse", "checkin", "write_object",
    "tree_rewrite", "peer_split", "touched_own_split", "reach_seeds", "reach_paths", "foreign_dirt",
)
FS_KINDS = ("open", "stat", "scan")


def _git(args: list, cwd: Path, stdin: bytes | None = None) -> str:
    result = run_git(["-c", "core.longpaths=true", *args], cwd=str(cwd), input=stdin)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _mod_path(i: int) -> str:
    return f"src/pkg{i % PKG_COUNT:02d}/mod{i:05d}.py"


def _mod_text(i: int) -> str:
    body = "".join(f"def f{j}(x):\n    return x + {i + j}\n\n" for j in range(12))
    return f"\"\"\"module {i}\"\"\"\nimport os\n\nVALUE = {i}\n\n{body}"


def _stream(dirty: int, sibling_tests: int) -> bytes:
    """A `git fast-import` stream: `dirty` tracked modules, a package test importing each, a README, and
    for the first `sibling_tests` modules the `tests/test_<stem>.py` beside it (`seeds_for_landing`'s seed)
    importing the module and the next REACH_FANOUT modules of its package."""
    files = {"README.md": b"bench\n"}
    for i in range(dirty):
        files[_mod_path(i)] = _mod_text(i).encode()
    for i in range(min(sibling_tests, dirty)):
        pkg = f"src.pkg{i % PKG_COUNT:02d}"
        imports = "".join(
            f"import {pkg}.mod{j:05d}\n" for j in range(i, min(dirty, i + PKG_COUNT * (REACH_FANOUT + 1)), PKG_COUNT)
        )
        files[f"src/pkg{i % PKG_COUNT:02d}/tests/test_mod{i:05d}.py"] = (imports + "\n\ndef test_ok():\n    assert True\n").encode()
    for p in range(PKG_COUNT):
        imports = "".join(
            f"import src.pkg{p:02d}.mod{i:05d}\n" for i in range(p, dirty, PKG_COUNT)
        )
        files[f"tests/test_pkg{p:02d}.py"] = (imports + "\n\ndef test_ok():\n    assert True\n").encode()
    body = b"seed"
    out = [f"commit refs/heads/main\ncommitter t <t@example.com> {EPOCH} +0000\ndata {len(body)}\n".encode(), body + b"\n"]
    for path, data in files.items():
        out.append(f"M 644 inline {path}\ndata {len(data)}\n".encode() + data + b"\n")
    return b"".join(out)


def _run_id(slug: str, i: int, now: float) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now - (i + 1) * STAMP_STEP_S))
    return f"warp-{slug}-{stamp}-{hashlib.sha1(f'{slug}{i}'.encode()).hexdigest()[:6]}"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _write_untracked(root: Path, untracked: int) -> None:
    """`untracked` small record files under `state/records/`, none tracked: the field shape of a tree
    whose dirt is mostly untracked records."""
    for i in range(untracked):
        rec = root / "state" / "records" / f"d{i % 100:02d}" / f"rec-{i:06d}.md"
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(f"record {i}\n", encoding="utf-8", newline="\n")


def build_fixture(parent: Path, name: str, *, dirty: int, runs: int, paths: int, untracked: int = 0) -> dict:
    """A repo with `dirty` tracked modules all modified in the worktree, `untracked` untracked record
    files, a focal run that declares and measures the first `paths` of them, and `runs` peer run records
    under `state/warp`: about
    HALTED_SHARE halted (marker, withheld set, withheld-index entry), the rest live (live-run index
    footprint), each declaring a rotating slice of the dirty paths, every seventh co-declaring the
    focal run's own paths. Returns `{root, run_id, paths}`."""
    from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index, live_run_index
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.measured_rev import _blob_sha

    root = parent / name
    root.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], root)
    _git(["fast-import", "--quiet"], root, stdin=_stream(dirty, paths))
    _git(["checkout", "-q", "-f", "main"], root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"),
                       ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
        _git(["config", key, value], root)
    mods = [_mod_path(i) for i in range(dirty)]
    peer_text = {i: _mod_text(i).replace("VALUE =", f"PEER = {i}\nVALUE =", 1) for i in range(dirty)}
    for i, rel in enumerate(mods):
        carries_peer = i < paths and i % PEER_HUNK_EVERY != 0
        base_text = peer_text[i] if carries_peer else _mod_text(i)
        (root / rel).write_text(base_text + f"EDIT = {i}\n", encoding="utf-8", newline="\n")
    _write_untracked(root, untracked)
    measured = mods[:paths]
    now = time.time()
    focal = _run_id("bench-measured-rev", 0, now)
    baseline = {}
    for i, rel in enumerate(measured):
        text = peer_text[i] if i % PEER_HUNK_EVERY != 0 else _mod_text(i)
        data = text.encode()
        baseline[rel] = _git(["hash-object", "-w", "--stdin"], root, stdin=data).strip()
        assert baseline[rel] == _blob_sha(data)
    _write_json(record_dir(root, focal) / "manifest.json", {
        "rows": [{"id": "C0", "writes": measured}], "write_baseline": baseline,
    })
    for i in range(runs):
        peer = _run_id("bench-peer", i + 1, now)
        declared = [mods[(i * 7 + k) % dirty] for k in range(6)]
        if i % 7 == 0:
            declared += measured[: 1 + i % 4]
        rdir = record_dir(root, peer)
        _write_json(rdir / "manifest.json", {"rows": [{"id": "P", "writes": declared}], "write_baseline": {}})
        if i < runs * HALTED_SHARE:
            halted_run.write_halt_marker(rdir, halted="bench_halt", sizing_rel=None)
            held = declared[:3] + (measured[:2] if i % 7 == 0 else [])
            halted_run.record_withheld(rdir, {p: "withheld" for p in held})
        else:
            live_run_index.stage(root, peer, declared, minted_at=now - (i + 1) * STAMP_STEP_S)
    halted_withheld_index.rebuild(root)
    return {"root": root, "run_id": focal, "paths": measured}


def _handler_call(fx: dict) -> Callable[[], Any]:
    from coordinator_core.ops.dispatch_emit import measured_rev

    return lambda: measured_rev._handler(
        {"run_id": fx["run_id"], "paths": list(fx["paths"])}, repo_root=fx["root"] / ".git",
    )


class PhaseMeter:
    """Exclusive process time, wall time, spawns and filesystem calls per phase; an unwrapped remainder is `other`."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.stack: list = []

    def row(self, name: str) -> dict:
        return self.rows.setdefault(name, {
            "calls": 0, "process_ms": 0.0, "incl_ms": 0.0, "wall_ms": 0.0, "spawns": 0,
            **{k: 0 for k in FS_KINDS},
        })

    def count(self, kind: str) -> None:
        self.row(self.stack[-1][0] if self.stack else "other")[kind] += 1

    def wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(*a: Any, **k: Any) -> Any:
            frame: list[Any] = [name, 0.0, 0.0]
            self.stack.append(frame)
            cpu0, wall0 = time.process_time(), time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                cpu = (time.process_time() - cpu0) * 1000.0
                wall = (time.perf_counter() - wall0) * 1000.0
                self.stack.pop()
                r = self.row(name)
                r["calls"] += 1
                r["incl_ms"] += cpu
                r["process_ms"] += cpu - frame[1]
                r["wall_ms"] += wall - frame[2]
                if self.stack:
                    self.stack[-1][1] += cpu
                    self.stack[-1][2] += wall
        return run


@contextmanager
def _spawn_counter(on_spawn: Callable[[str], None]) -> Iterator[None]:
    real = subprocess.Popen.__init__

    def counting(self: Any, *a: Any, **k: Any) -> None:
        argv = a[0] if a else k.get("args")
        on_spawn(" ".join(map(str, argv[:3])) if isinstance(argv, (list, tuple)) else str(argv)[:60])
        return real(self, *a, **k)

    subprocess.Popen.__init__ = counting
    try:
        yield
    finally:
        subprocess.Popen.__init__ = real


@contextmanager
def _fs_counter(meter: PhaseMeter) -> Iterator[None]:
    import builtins
    import io

    real_open, real_io_open = builtins.open, io.open
    real_os = {name: getattr(os, name) for name in ("stat", "lstat", "scandir", "listdir")}

    def opened(*a: Any, **k: Any) -> Any:
        meter.count("open")
        return real_open(*a, **k)

    def counted(kind: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*a: Any, **k: Any) -> Any:
            meter.count(kind)
            return real(*a, **k)
        return call

    kinds = {"stat": "stat", "lstat": "stat", "scandir": "scan", "listdir": "scan"}
    setattr(builtins, "open", opened)
    setattr(io, "open", opened)
    for name, real in real_os.items():
        setattr(os, name, counted(kinds[name], real))
    try:
        yield
    finally:
        setattr(builtins, "open", real_open)
        setattr(io, "open", real_io_open)
        for name, real in real_os.items():
            setattr(os, name, real)


@contextmanager
def _phase_wrappers(meter: PhaseMeter) -> Iterator[None]:
    from coordinator_core.git import commit
    from coordinator_core.ops.dispatch_emit import hunk_provenance, landing_integrity, measured_rev, unclaimed_dirt_stage

    targets = (
        (measured_rev, "_run_landing_paths", "run_landing_paths"),
        (measured_rev, "_drop_unmeasurable", "drop_unmeasurable"),
        (measured_rev, "read_tree_spine", "spine"),
        (landing_integrity, "_cached_parse_finding", "py_parse"),
        (commit, "_checkin_bytes", "checkin"),
        (measured_rev, "write_object", "write_object"),
        (measured_rev, "_rewrite_head_spine", "tree_rewrite"),
        (hunk_provenance, "peer_split", "peer_split"),
        (unclaimed_dirt_stage, "touched_own_split", "touched_own_split"),
        (measured_rev, "seeds_for_landing", "reach_seeds"),
        (measured_rev, "reach_paths", "reach_paths"),
        (measured_rev, "foreign_dirt", "foreign_dirt"),
    )
    saved = [(mod, attr, getattr(mod, attr)) for mod, attr, _ in targets]
    for mod, attr, phase in targets:
        setattr(mod, attr, meter.wrap(phase, getattr(mod, attr)))
    try:
        yield
    finally:
        for mod, attr, original in saved:
            setattr(mod, attr, original)


def _outcome(result) -> str:
    if isinstance(result, dict) and "sha" in result:
        return "ok"
    if isinstance(result, dict):
        return f"refused:{str(result.get('error'))[:160]}"
    return f"unexpected:{type(result).__name__}"


def measure(call) -> dict:
    """One call inside a process-time + descendants bracket; spawns are `Popen` constructions."""
    spawns: list = []
    child = _ChildMeter()
    with _spawn_counter(spawns.append):
        child.start()
        cpu0, wall0 = time.process_time(), time.perf_counter()
        try:
            result = call()
        finally:
            process_ms = round((time.process_time() - cpu0) * 1000.0 + child.stop_ms(), 3)
            wall_ms = (time.perf_counter() - wall0) * 1000.0
    return {
        "process_ms": process_ms, "spawns": len(spawns), "spawn_sites": spawns[:10],
        "wall_minus_process_ms": round(max(wall_ms - process_ms, 0.0), 3), "outcome": _outcome(result),
        "changed": len(result.get("blobs", {})) if isinstance(result, dict) else 0,
        "reach_dirt": len(result.get("reach_dirt", {})) if isinstance(result, dict) else 0,
        "peer_hunks": len(result.get("peer_hunks", {})) if isinstance(result, dict) else 0,
    }


def attribute(call: Callable[[], Any], repeats: int) -> dict:
    """The per-call mean phase table over `repeats` instrumented calls on one fixture (Windows process
    time ticks at ~15.6ms, so a single bracket cannot see a phase under a tick; N calls in one window
    divided by N can): every phase row plus `other` and the instrumented total."""
    meter = PhaseMeter()
    for name in PHASES:
        meter.row(name)
    meter.row("other")
    spawns: list = []

    def on_spawn(site: str) -> None:
        spawns.append(site)
        meter.count("spawns")

    with ExitStack() as stack:
        stack.enter_context(_spawn_counter(on_spawn))
        stack.enter_context(_phase_wrappers(meter))
        stack.enter_context(_fs_counter(meter))
        cpu0 = time.process_time()
        for _ in range(repeats):
            result = call()
        total = (time.process_time() - cpu0) * 1000.0
    meter.rows["other"]["process_ms"] = max(total - sum(r["process_ms"] for n, r in meter.rows.items() if n != "other"), 0.0)
    rows = {
        n: {k: round(v / repeats, 3) for k, v in r.items()} for n, r in meter.rows.items()
    }
    return {
        "repeats": repeats, "instrumented_total_ms": round(total / repeats, 3), "spawns_total": len(spawns),
        "outcome": _outcome(result), "phases": rows,
    }


def load_at_head(module_names: list) -> list:
    """Re-execute each named `coordinator_core` module from `git show HEAD:<file>` inside the live module
    namespace, so a module a peer has edited in the shared tree measures as committed. Classes the module
    defines are re-created, so a module that already imported one keeps the old object; use it for
    function-level phases only. Returns the `{module, bytes}` it replaced."""
    import importlib

    replaced = []
    for name in module_names:
        mod = importlib.import_module(name)
        file = str(mod.__file__)
        rel = Path(file).resolve().relative_to(_REPO).as_posix()
        source = _git(["show", f"HEAD:{rel}"], _REPO)
        exec(compile(source, file, "exec"), mod.__dict__)  # noqa: S102 -- committed source of an engine module
        replaced.append({"module": name, "bytes": len(source)})
    return replaced


def run(workdir: Path, dirty: int, runs: int, paths: int, repeats: int, at_head: list, untracked: int = 0) -> dict:
    """Warm-up, the enforced first call, a repeat call, then `repeats` attributed calls on a fresh fixture."""
    declare_benchmark_origin()
    out: dict = {"dirty": dirty, "runs": runs, "paths": paths, "untracked": untracked, "bar_ms": BAR_MS}
    warm = build_fixture(workdir, "warmup", dirty=8, runs=4, paths=4)
    _handler_call(warm)()
    out["at_head"] = load_at_head(at_head)
    first_fx = build_fixture(workdir, "first", dirty=dirty, runs=runs, paths=paths, untracked=untracked)
    out["first"] = measure(_handler_call(first_fx))
    out["repeat"] = measure(_handler_call(first_fx))
    attr_fx = build_fixture(workdir, "attribution", dirty=dirty, runs=runs, paths=paths, untracked=untracked)
    out["attribution"] = attribute(_handler_call(attr_fx), repeats)
    return out


def render(result: dict) -> str:
    first, repeat, attr = result["first"], result["repeat"], result["attribution"]
    lines = [
        f"fixture: dirty={result['dirty']} untracked={result['untracked']} runs={result['runs']} "
        f"measured_paths={result['paths']} "
        f"at_head={[m['module'] for m in result['at_head']] or 'none (tree as edited)'}",
        f"first  : process_ms={first['process_ms']:.1f} spawns={first['spawns']} "
        f"wall_minus_process_ms={first['wall_minus_process_ms']:.1f} changed={first['changed']} "
        f"reach_dirt={first['reach_dirt']} peer_hunks={first['peer_hunks']} outcome={first['outcome']}",
        f"repeat : process_ms={repeat['process_ms']:.1f} spawns={repeat['spawns']} "
        f"wall_minus_process_ms={repeat['wall_minus_process_ms']:.1f} outcome={repeat['outcome']}",
        f"bar    : process_ms < {BAR_MS:.0f}, spawns == 0, no process > {PROCESS_BAR_MS:.0f}ms "
        f"(phase table: per-call mean of {attr['repeats']} instrumented calls, total "
        f"{attr['instrumented_total_ms']:.1f}ms, outcome={attr['outcome']})",
        f"{'phase':20} {'calls':>6} {'process_ms':>10} {'incl_ms':>9} {'wall_ms':>9} {'spawns':>6} "
        f"{'open':>7} {'stat':>7} {'scan':>6}",
    ]
    for name, r in sorted(attr["phases"].items(), key=lambda kv: -kv[1]["process_ms"]):
        lines.append(
            f"{name:20} {r['calls']:6.0f} {r['process_ms']:10.1f} {r['incl_ms']:9.1f} {r['wall_ms']:9.1f} "
            f"{r['spawns']:6.0f} {r['open']:7.0f} {r['stat']:7.0f} {r['scan']:6.0f}"
        )
    return "\n".join(lines)


def over_bar(result: dict) -> list:
    """Why pass `first` fails the bar; empty when it holds."""
    first = result["first"]
    why = []
    if first["outcome"] != "ok":
        why.append(f"no candidate commit: {first['outcome']}")
    if first["process_ms"] >= BAR_MS:
        why.append(f"process_ms {first['process_ms']:.1f} >= {BAR_MS:.0f}")
    if first["spawns"]:
        why.append(f"spawns {first['spawns']} > 0")
    return why


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dirty", type=int, default=DEFAULT_DIRTY, help="tracked paths dirty in the worktree")
    ap.add_argument("--untracked", type=int, default=0, help="untracked record files under state/records")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="peer run records under state/warp")
    ap.add_argument("--paths", type=int, default=DEFAULT_PATHS, help="paths the focal run declares and measures")
    ap.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="attributed calls averaged into the phase table")
    ap.add_argument(
        "--at-head", action="append", default=[], metavar="MODULE",
        help="dotted coordinator_core module to measure as committed at HEAD, not as edited in the tree (repeatable)",
    )
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit {EXIT_OVER_BAR} over the bar")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not 0 < args.paths <= args.dirty:
        ap.error("--paths must be between 1 and --dirty")
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.temp_layout import make_managed_temp_dir

    workdir = make_managed_temp_dir(prefix="mrdt-")
    try:
        with fixture_settings_home(workdir):
            result = run(workdir, args.dirty, args.runs, args.paths, max(args.repeat, 1), args.at_head, args.untracked)
    finally:
        rmtree_or_raise(workdir, label="measured-rev-dirty-tree")
    why = over_bar(result)
    result["over_bar"] = why
    print(json.dumps(result, indent=1) if args.json else render(result) + (f"\nOVER BAR: {'; '.join(why)}" if why else "\nunder bar"))
    return EXIT_OVER_BAR if args.enforce_bar and why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
