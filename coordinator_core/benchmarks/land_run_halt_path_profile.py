"""Process-time and spawn profile of the `dispatch.land_run` halt path over N `state/warp` run dirs.

One default-param `dispatch.land_run` of a halting run (`halted_by` envelope, no token, no tuning
knobs), resolved through the op registry, over a fixture repo whose `state/warp` holds N stamped
`ask-*` run dirs: mostly stale, plus a handful of fresh token-live runs and fresh halted runs that
withhold dirty paths. Process time is this process plus descendants (`commit_leg_profile._ChildMeter`);
wall clock is never recorded. One JSON row per N:
`{axis, run_dirs, process_time_ms, spawns, outcome, committed, reply_returned, withheld_paths,
stall_ms, retry_safe, attribution}`.

    python coordinator_core/benchmarks/land_run_halt_path_profile.py [--run-dirs N ...] [--attribute]
        [--withheld-paths N] [--stall-ms M]

`--run-dirs` repeats; the default is `1000` and `5000`. `--attribute` adds a cProfile top-15 of
`coordinator_core` functions per row. `--withheld-paths N` declares N extra dirty paths on the target
run's chunk (one row, at `WITHHELD_RUN_DIRS` run dirs unless `--run-dirs` is given). `--stall-ms M` blocks
the first halt-path file read M ms (in-process monkeypatch of the read primitives, never in prod code);
`reply_returned` is true when the call answered a dict before that stall ended. Exit 2 when any row is
over `BAR_MS` (DR-344), else 0. The fixture lives under `<repo>/scratch/` and is removed on exit; the
settings home is pinned inside it.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home  # noqa: E402
from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter  # noqa: E402

AXIS = "dispatch.land_run.halt"
BAR_MS = 500.0
DEFAULT_RUN_DIRS = (1000, 5000)
FRESH_TOKEN_RUNS = 3
FRESH_HALTED_RUNS = 4
WITHHELD_PER_HALTED_RUN = 3
OWN_PY_PATHS = 3
SELF = "5e1f0000-0000-4000-8000-0000000000c1"
TARGET_SUFFIX = "aaaaaa"
STALE_SIZING = "state/sizings/stale.yaml"
WARMUP_RUN_DIRS = 5
TOP_FUNCTIONS = 15
WITHHELD_RUN_DIRS = 30


@dataclass
class HaltRow:
    """One measured N. `reply_returned`: a dict reply came back before the injected stall ended (no
    stall: any dict reply)."""

    axis: str
    run_dirs: int
    process_time_ms: float = 0.0
    spawns: int = 0
    outcome: str = ""
    committed: bool = False
    attribution: dict = field(default_factory=dict)
    reply_returned: bool = False
    withheld_paths: int = 0
    stall_ms: int = 0
    retry_safe: bool = False


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%S")


def _stale_dirs(root: Path, count: int) -> None:
    """`count` stamped run dirs, four in five well outside every token TTL: a `run.json` each, one in five a `halted.json`."""
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    for i in range(count):
        # One in five sits inside the token TTL with no token: opened by the scan, claiming nothing.
        moment = now - timedelta(minutes=5 + i % 1300) if i % 5 == 4 else start + timedelta(minutes=i)
        run_id = f"ask-{_stamp(moment)}-{i:06x}"
        _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", json.dumps({"run_id": run_id, "sizing_rel": STALE_SIZING}))
        if i % 5 == 0:
            _write(root, f"{RECORD_DIR_ROOT}/{run_id}/halted.json",
                   json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": STALE_SIZING}))


def _fresh_runs(root: Path) -> None:
    """Token-live runs (minted) and halted runs whose withheld paths are dirty in the worktree."""
    from coordinator_core.ops.dispatch_emit import halted_run, run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    now = datetime.now(timezone.utc)
    for i in range(FRESH_TOKEN_RUNS):
        run_id = f"ask-{_stamp(now - timedelta(minutes=2 + i))}-{0xf00 + i:06x}"
        _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", json.dumps({"run_id": run_id}))
        run_token.mint(root, run_id, f"run-fresh-{i}.mjs")
    for i in range(FRESH_HALTED_RUNS):
        run_id = f"ask-{_stamp(now - timedelta(minutes=10 + i))}-{0xe00 + i:06x}"
        record_dir = root / RECORD_DIR_ROOT / run_id
        _write(root, f"{RECORD_DIR_ROOT}/{run_id}/run.json", json.dumps({"run_id": run_id}))
        withheld = {}
        for j in range(WITHHELD_PER_HALTED_RUN):
            rel = f"peerwork/h{i}/w{j}.py"
            _write(root, rel, f"VALUE = {i * 10 + j}\n")
            withheld[rel] = "held"
        halted_run.write_halt_marker(record_dir, halted="ledger-check", sizing_rel=None)
        halted_run.record_withheld(record_dir, withheld)


def build_fixture(workdir: Path, run_dirs: int, withheld_paths: int = 0) -> tuple:
    """A git repo `workdir/repo` with `run_dirs` stale run dirs, the fresh runs, the withheld index built,
    and the target run's record dir, script and one done chunk that also declares `withheld_paths`
    untracked dirty files the halt probes and withholds. Returns `(root, run_id, params)`."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT, RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    root = workdir / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
        _git(root, "config", key, value)
    _write(root, "docs/plans/seed.md", "seed\n")
    _write(root, "archive/.keep", "onboarded\n")  # `repo_standing.is_onboarded`: an unonboarded repo withholds every run artifact
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")

    _stale_dirs(root, run_dirs)
    _fresh_runs(root)
    halted_withheld_index.rebuild(root)

    run_id = f"ask-{_stamp(datetime.now(timezone.utc))}-{TARGET_SUFFIX}"
    for name in ("ask.md", "run.json"):
        _write(root, f"{RECORD_DIR_ROOT}/{run_id}/{name}", f"{name}\n")
    _write(root, f"{RUN_DIR_ROOT}/{run_id}/submission.json", json.dumps({"submitted_at": time.time() - 60.0}))  # `register_gate.run_started_at`: the window `_run_written` reads touch records from
    _write(root, "product/done.py", "DONE = 1\n")
    _write(root, "reports/C1.md", "DONE: C1\n")
    extra = tuple(f"withheld/w{i:04d}.py" for i in range(withheld_paths))
    for rel in extra:
        _write(root, rel, f"VALUE = {rel!r}\n")
    marker = render_marker(CommitRequest(
        chunks=(ChunkCommit(id="C1", title="C1", paths=("product/done.py", *extra), report="reports/C1.md"),),
        session_id=None, repo_root=None, plan_path=None,
    ))
    _write(root, "run.mjs", f"{marker}\n")
    _own_dirt(root, run_id)
    params = {"script_path": "run.mjs", "run_id": run_id, "session_id": SELF,
              HALTED_RECORD_PARAM: {"halted": "ledger-check", "reason": "row R1 open"}}
    return root, run_id, params


def _own_dirt(root: Path, run_id: str) -> None:
    """`OWN_PY_PATHS`: tracked `.py` the run's session touched and edited past HEAD, which no chunk
    declares and whose manifest baseline is HEAD, so the halt parks each in its record through
    `halt_tree_evict.evict`, which records the path's tree bytes and writes nothing to the tree."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.session import core as session_core
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record
    from coordinator_core.session_hub_path import session_dir_for

    session_core.init(SELF, cwd=str(root))
    sink = touch_record.sink_path(session_dir_for(root / ".git", SELF))
    rels = [f"product/own{i}.py" for i in range(OWN_PY_PATHS)]
    baseline = {}
    for i, rel in enumerate(rels):
        body = f'"""Run-owned module {i}."""\n\nVALUE_{i} = {i}\n'
        _write(root, rel, body)
        baseline[rel] = git_objects.write_object(resolve_git_common_dir(root), b"blob", body.encode())
    _git(root, "add", "--", *rels)
    _git(root, "commit", "-q", "-m", "own modules at the run's baseline")
    _write(root, f"{RECORD_DIR_ROOT}/{run_id}/manifest.json",
           json.dumps({"rows": [], "doc_writes": [], "write_baseline": baseline}))
    for i, rel in enumerate(rels):
        _write(root, rel, f'"""Run-owned module {i}."""\n\nVALUE_{i} = {i}\nEDITED_{i} = True\n')
        session_scope.touch(SELF, rel, cwd=str(root))
        touch_record.append_event(
            sink, session_id=SELF, agent_id=None, verb="T", path=rel,
            content_hash=touch_record.compute_content_hash(root / rel), kind="w",
        )


def _call_halt(root: Path, params: dict) -> dict:
    """`dispatch.land_run` through the re-entry seam. An unregistered op raises OpUnavailableError."""
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    return reentrant_dispatch("dispatch.land_run", dict(params), repo_root=root / ".git")


def _top_functions(prof: cProfile.Profile) -> list:
    rows = []
    for (fname, _line, name), (_cc, nc, tt, ct, _c) in getattr(pstats.Stats(prof), "stats").items():
        f = fname.replace("\\", "/")
        if "coordinator_core/" in f and "benchmarks/" not in f:
            rows.append((ct, f"{f.split('coordinator_core/', 1)[1]}:{name}", nc, tt))
    rows.sort(reverse=True)
    return [{"cum_ms": round(ct * 1000, 1), "fn": fn, "calls": nc, "self_ms": round(tt * 1000, 1)}
            for ct, fn, nc, tt in rows[:TOP_FUNCTIONS]]


class _Stall:
    """The one-shot stall: `end` is the monotonic moment the blocked read releases, None until it fires."""

    def __init__(self, stall_ms: int) -> None:
        self.stall_ms = stall_ms
        self.end: "float | None" = None
        self.path = ""
        self._lock = threading.Lock()

    def fire(self, path: object) -> None:
        with self._lock:
            if self.end is not None:
                return
            self.end = time.monotonic() + self.stall_ms / 1000.0
            self.path = str(path)
        time.sleep(self.stall_ms / 1000.0)


@contextmanager
def stalled_first_read(stall_ms: int) -> Generator["_Stall | None"]:
    """Block the first halt-path file read `stall_ms` ms by wrapping the read primitives in-process
    (`call_io_scope._read`, `bounded_io._read_registered`, `Path.read_bytes`); a bounded read stalls on
    its worker thread, an unbounded one on the caller. Yields the `_Stall`, None when `stall_ms` is 0."""
    if stall_ms <= 0:
        yield None
        return
    from coordinator_core import bounded_io
    from coordinator_core.git import call_io_scope

    stall = _Stall(stall_ms)
    real_read, real_registered, real_path_read = call_io_scope._read, bounded_io._read_registered, Path.read_bytes

    def read(path: str, bounded: bool = False) -> "bytes | None":
        if not bounded and call_io_scope._BOUNDED_DEADLINE.get() is None:  # a bounded read reaches `_read_registered` on its worker
            stall.fire(path)
        return real_read(path, bounded)

    def registered(path: str) -> "bytes | None":
        stall.fire(path)
        return real_registered(path)

    def path_read(self: Path) -> bytes:
        stall.fire(self)
        return real_path_read(self)

    patches = ((call_io_scope, "_read", read, real_read), (bounded_io, "_read_registered", registered, real_registered),
               (Path, "read_bytes", path_read, real_path_read))
    for owner, name, new, _old in patches:
        setattr(owner, name, new)
    try:
        yield stall
    finally:
        for owner, name, _new, old in patches:
            setattr(owner, name, old)


def measure(root: Path, params: dict, run_dirs: int, *, attribute: bool = False, stall_ms: int = 0,
            withheld_paths: int = 0) -> HaltRow:
    """Run the halt land once and return its row; a raised op is a row (`outcome` `raised:<Type>`)."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    halted_withheld_index.forget_fresh()
    spawns = [0]
    real_init = subprocess.Popen.__init__

    def popen_init(self, *a, **k) -> None:
        spawns[0] += 1
        return real_init(self, *a, **k)

    prof = cProfile.Profile(time.process_time) if attribute else None
    meter = _ChildMeter()
    result, error = None, ""
    subprocess.Popen.__init__ = popen_init
    with stalled_first_read(stall_ms) as stall:
        meter.start()
        cpu0 = time.process_time()
        try:
            if prof is not None:
                prof.enable()
            try:
                result = _call_halt(root, params)
            finally:
                if prof is not None:
                    prof.disable()
        except BaseException as exc:  # noqa: BLE001 -- a raised op is a row, not a crash of the harness
            error = type(exc).__name__
        finally:
            returned_at = time.monotonic()
            own_ms = (time.process_time() - cpu0) * 1000.0
            total_ms = round(own_ms + meter.stop_ms(), 3)
            subprocess.Popen.__init__ = real_init
    reply = result if isinstance(result, dict) else {}
    outcome = f"raised:{error}" if error else (reply.get("refused") or ("committed" if reply.get("committed") else "not-committed"))
    in_time = stall is None or stall.end is None or returned_at < stall.end
    row = HaltRow(AXIS, run_dirs, total_ms, spawns[0], str(outcome), reply.get("committed") is True,
                  reply_returned=bool(reply) and in_time, withheld_paths=withheld_paths, stall_ms=stall_ms,
                  retry_safe=reply.get("retry_safe") is True)
    if stall is not None and stall.path:
        row.attribution["stalled_read"] = stall.path
    if reply.get("error"):
        row.attribution["error"] = str(reply["error"])[:300]
    if prof is not None:
        row.attribution["top_functions"] = _top_functions(prof)
    return row


def profile_run_dirs(run_dirs: int, scratch: Path, *, attribute: bool, withheld_paths: int = 0,
                     stall_ms: int = 0) -> HaltRow:
    """Build a fixture of `run_dirs` stale dirs under `scratch`, warm the engine on a tiny one, then
    measure one halt land on the real one (the warm-up never stalls)."""
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
    from coordinator_core.temp_layout import mkdtemp_in

    row = HaltRow(AXIS, run_dirs)
    for count, measured in ((WARMUP_RUN_DIRS, False), (run_dirs, True)):
        workdir = mkdtemp_in("lrhp-", scratch)
        try:
            with fixture_settings_home(workdir):
                root, _run_id, params = build_fixture(workdir, count, withheld_paths if measured else 0)
                one = measure(root, params, count, attribute=attribute and measured,
                              stall_ms=stall_ms if measured else 0, withheld_paths=withheld_paths if measured else 0)
                if measured:
                    row = one
        finally:
            rmtree_or_raise(workdir, label="land-run-halt-path-profile")
    return row


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dirs", type=int, action="append", metavar="N",
                    help=f"stale `state/warp` run dirs in the fixture; repeatable (default {list(DEFAULT_RUN_DIRS)})")
    ap.add_argument("--attribute", action="store_true", help="cProfile top-15 coordinator_core functions per row")
    ap.add_argument("--withheld-paths", type=int, metavar="N",
                    help=f"dirty paths the target chunk declares; one row at {WITHHELD_RUN_DIRS} run dirs unless --run-dirs")
    ap.add_argument("--stall-ms", type=int, default=0, metavar="M",
                    help="block the first halt-path file read M ms (in-process seam)")
    args = ap.parse_args(argv)
    declare_benchmark_origin()
    os.environ["COORDINATOR_SESSION_ID"] = SELF
    os.environ.pop("CLAUDE_SESSION_ID", None)
    os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    scratch = _REPO / "scratch"
    scratch.mkdir(exist_ok=True)
    shaped = args.withheld_paths is not None or args.stall_ms > 0
    sizes = args.run_dirs or ((WITHHELD_RUN_DIRS,) if shaped else DEFAULT_RUN_DIRS)
    rows = [profile_run_dirs(n, scratch, attribute=args.attribute, withheld_paths=args.withheld_paths or 0,
                             stall_ms=args.stall_ms) for n in sizes]
    print(json.dumps([asdict(r) for r in rows], indent=1))
    over = [r for r in rows if r.process_time_ms > BAR_MS]
    if over:
        print(f"KILL-GATE: {AXIS} over {BAR_MS:.0f}ms process time (DR-344) at N={[r.run_dirs for r in over]}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
