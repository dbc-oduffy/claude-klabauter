"""Brightline profile of the land gate's reach check: `dispatch.measured_rev` and `dispatch.land_run`
over a landing set of three real `.py` files, one under `coordinator_core/ops/dispatch_emit/` (the
widest import closure and largest test directory), on throwaway clones of this repo.

Process time is this process plus descendants (`Profiler.tree_ms`); wall clock is never recorded.
`spawns` counts `Popen` constructions. `added_git_spawns` counts those made inside
`landing_reach.reach_paths` / `foreign_dirt` (0 when the module is absent, as at the baseline).
One row per axis; the bar is `BAR_MS` per op.

    python coordinator_core/benchmarks/land_gate_reach_profile.py [--json] [--source-repo PATH]

The code under test is the tree this file sits in; copying the file into a `git archive` of an older
commit measures that commit (`--source-repo` names the live repo whose HEAD the clones carry).
`land_run.reach_check` times the reach walk alone over the same landing set in the land clone; it is
inside the `dispatch.land_run` row.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home  # noqa: E402
from coordinator_core.benchmarks.isolated_clone import fresh_git_clone  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.benchmarks.land_run_leg_budget_profile import Profiler  # noqa: E402
from coordinator_core.benchmarks.land_run_slow_disk_profile import (  # noqa: E402
    DEFAULT_CHAIN,
    DEFAULT_TRACKED,
    PATH_P,
    PLAN,
    RUN_ID,
    SCRIPT,
    SELF,
    _bind_session,
    _git,
    _outcome,
    _write,
    build_land_fixture,
)

BAR_MS = 500.0
MEASURED_REV = "dispatch.measured_rev"
LAND_RUN = "dispatch.land_run"
REACH_CHECK = "land_run.reach_check"
LANDING_SET = (
    "coordinator_core/ops/dispatch_emit/measured_rev.py",
    "coordinator_core/pyimports.py",
    "coordinator_core/git/git_index.py",
)
MARK = "\n# land_gate_reach_profile: dirty edit\n"
_REACH_FNS = ("reach_paths", "foreign_dirt", "seeds_for_landing")
_ATTRIBUTED_FILES = ("measured_rev.py", "land_run.py", "landing_reach.py")
_TOP_FRAMES = 8
_MEASURED_REV_LEGS = {
    "parse": ("unparseable_py",),
    "spine_read": ("read_tree_spine",),
    "blob_writes": ("_checkin_bytes", "write_object"),
    "spine_rewrite": ("_synthesize_absent_spine_dirs", "_rewrite_head_spine"),
    "reach": ("reach_paths", "foreign_dirt", "seeds_for_landing"),
}


def _profiled_attribution(call, legs: dict | None = None) -> dict:
    """Run `call()` once under cProfile and return `{"legs": ..., "top_frames": ...}` in profiled ms.
    A pass separate from `_measure`: cProfile inflates process time, so these numbers locate cost and
    never stand in for `process_time_ms`."""
    prof = cProfile.Profile()
    error = ""
    try:
        prof.runcall(call)
    except Exception as exc:  # noqa: BLE001 -- a raised op still leaves its frames; Ctrl-C must still abort
        error = type(exc).__name__
    stats = pstats.Stats(prof).stats
    frames = {}
    by_name: dict = {}
    for (filename, _line, name), (_cc, _nc, _tt, ct, _callers) in stats.items():
        by_name[name] = by_name.get(name, 0.0) + ct
        base = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if base in _ATTRIBUTED_FILES:
            frames[f"{base}:{name}"] = round(ct * 1000.0, 3)
    top = dict(sorted(frames.items(), key=lambda kv: -kv[1])[:_TOP_FRAMES])
    out = {"profiled": True, "top_frames": top}
    if legs:
        out["legs"] = {leg: round(sum(by_name.get(n, 0.0) for n in names) * 1000.0, 3) for leg, names in legs.items()}
    if error:
        out["error"] = error
    return out


@dataclass
class Row:
    """One measured axis."""

    axis: str
    process_time_ms: float = 0.0
    spawns: int = 0
    added_git_spawns: int = 0
    attribution: dict = field(default_factory=dict)


def _measure(axis: str, call, **attribution) -> tuple:
    """Run `call()` once inside a process-time + descendants bracket; returns `(row, result)`.
    Spawns are `Popen` constructions; those inside a `landing_reach` function are `added_git_spawns`."""
    prof = Profiler(attribute=False)
    state = {"spawns": 0, "added": 0, "depth": 0}
    real_init = subprocess.Popen.__init__

    def popen_init(self, *a, **k):
        state["spawns"] += 1
        state["added"] += 1 if state["depth"] else 0
        return real_init(self, *a, **k)

    undo = []
    try:
        from coordinator_core.ops.dispatch_emit import land_run, landing_reach, measured_rev  # noqa: F401
    except ImportError:
        landing_reach = None
        from coordinator_core.ops.dispatch_emit import measured_rev  # noqa: F401
    if landing_reach is not None:
        def counted(fn):
            def wrapper(*a, **k):
                state["depth"] += 1
                try:
                    return fn(*a, **k)
                finally:
                    state["depth"] -= 1

            return wrapper

        for mod in (landing_reach, measured_rev):
            for name in _REACH_FNS:
                if hasattr(mod, name):
                    real = getattr(mod, name)
                    setattr(mod, name, counted(real))
                    undo.append((mod, name, real))
    subprocess.Popen.__init__ = popen_init
    prof.meter.start()
    t0 = prof.tree_ms()
    cpu0 = time.process_time()
    result, error = None, ""
    try:
        result = call()
    except BaseException as exc:  # noqa: BLE001 -- a raised op is a row, not a crash of the harness
        error = type(exc).__name__
    finally:
        ms = round((time.process_time() - cpu0) * 1000.0 + prof.meter.stop_ms(), 3)
        tree_ms = round(prof.tree_ms() - t0, 3)
        subprocess.Popen.__init__ = real_init
        for mod, name, real in undo:
            setattr(mod, name, real)
    attribution["reach_module_present"] = landing_reach is not None
    if error:
        attribution["error"] = error
    return Row(axis, max(ms, tree_ms), state["spawns"], state["added"], attribution), result


def _dirty_landing_set(root: Path) -> None:
    for rel in LANDING_SET:
        with open(root / rel, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(MARK)


def apply_overlay(source: Path, root: Path, overlay) -> None:
    """Copy each repo-relative file of `overlay` from `source`'s working tree into the clone `root`, so the
    clone carries edits a commit does not yet hold."""
    for rel in overlay:
        src = Path(source) / rel
        if not src.is_file():
            raise FileNotFoundError(f"--overlay path is not a file in {source}: {rel}")
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)


def profile_measured_rev(source: Path, stack: ExitStack, overlay=()) -> Row:
    """One `measured_rev` over `LANDING_SET`, edited in a fresh clone of `source`'s HEAD (plus `overlay`).
    A twin clone runs first under cProfile: it is the warm-up (the engine holds its imports loaded, so a
    first import is not the op's cost) and the attribution."""
    from coordinator_core.ops.dispatch_emit import measured_rev

    twin = stack.enter_context(fresh_git_clone(source, prefix="lgrp-mrp", label="measured_rev attribution"))
    apply_overlay(source, twin, overlay)
    _dirty_landing_set(twin)
    profiled = _profiled_attribution(
        lambda: measured_rev.measured_rev("lgrp-run", LANDING_SET, repo_root=twin), _MEASURED_REV_LEGS)
    root = stack.enter_context(fresh_git_clone(source, prefix="lgrp-mr", label="measured_rev profile"))
    apply_overlay(source, root, overlay)
    _dirty_landing_set(root)
    row, result = _measure(MEASURED_REV, lambda: measured_rev.measured_rev("lgrp-run", LANDING_SET, repo_root=root),
                           landing_set=list(LANDING_SET))
    if isinstance(result, dict):
        row.attribution["blobs"] = len(result.get("blobs") or {})
        row.attribution["reach_dirt"] = len(result.get("reach_dirt") or {})
    row.attribution["profiled"] = profiled
    row.attribution["overlay"] = list(overlay)
    return row


CRLF, LF, NUL = bytes([13, 10]), bytes([10]), bytes([0])


def _copy_lf(src: str, dst: str) -> str:
    """`copytree` copy function: CRLF -> LF. The source clone is checked out under the box's global
    `core.autocrlf=true`; the live tree it stands for is LF, and CR bytes in the fixture would send every
    landing path through the `git hash-object` fallback spawn that a real landing never pays."""
    data = Path(src).read_bytes()
    if CRLF in data and NUL not in data:
        data = data.replace(CRLF, LF)
    Path(dst).write_bytes(data)
    shutil.copystat(src, dst)
    return dst


def build_land_clone(parent: Path, source: Path, stack: ExitStack, overlay=()) -> tuple:
    """The `build_land_fixture` clean landing, with the source repo's `coordinator_core` tree (plus `overlay`)
    committed into it and `LANDING_SET` dirtied, touched by the session and named by the emitted script's
    chunk."""
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.session import scope as session_scope
    from coordinator_core.session import touch_record

    real = stack.enter_context(fresh_git_clone(source, prefix="lgrp-src", label="land_run profile source"))
    apply_overlay(source, real, overlay)
    root, base = build_land_fixture(parent, mode="clean", tracked=DEFAULT_TRACKED, chain=DEFAULT_CHAIN, stack=stack)
    shutil.copytree(real / "coordinator_core", root / "coordinator_core", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"), copy_function=_copy_lf)
    for args in (["add", "--", "coordinator_core"], ["commit", "-q", "-m", "profile: real coordinator_core tree"]):
        done = run_git(args, cwd=str(root), remote=True)  # a bulk setup step: outside the seam's ceiling
        if not done.ok:
            raise RuntimeError(f"git {args[0]} failed in {root}: {done.stderr.strip()}")
    _dirty_landing_set(root)
    from coordinator_core.session_hub_path import session_dir_for

    sink = touch_record.sink_path(session_dir_for(root / ".git", SELF))
    for rel in LANDING_SET:
        session_scope.touch(SELF, rel, cwd=str(root))
        touch_record.append_event(
            sink, session_id=SELF, agent_id=None, verb="T", path=rel,
            content_hash=touch_record.compute_content_hash(root / rel), kind="w",
        )
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=(PATH_P, *LANDING_SET)),), plan_path=PLAN)
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return root, base


def _call_land_with_verdict(root: Path, base: str) -> dict:
    """`_call_land` with a recorded passing test verdict, so the landing's bulk retest gate (outside the
    bar) clears on the verdict instead of refusing the landing set's tests as not run."""
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

    from coordinator_core.ops.dispatch_emit import landing_integrity

    land_run._launch_close_job = lambda *a, **k: True
    landing_integrity.landing_invariant_nodes = lambda *a, **k: []  # the bulk invariant retest is outside the bar
    review = {**reviewed(tests={"status": "pass", "failed": 0, "run": 3}), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF, "run_id": RUN_ID, "run_base_sha": base},
        repo_root=root / ".git",
    )


def profile_land_run(parent: Path, source: Path, stack: ExitStack, overlay=()) -> list:
    """One `land_run` of the clean landing plus the reach walk alone over the same landing set. A twin
    landing runs first under cProfile: it is the warm-up (every import `land_run` reaches lazily is loaded,
    as in the warm engine) and the attribution."""
    twin_root, twin_base = build_land_clone(parent / "twin", source, stack, overlay)
    profiled = _profiled_attribution(
        lambda: _call_land_with_verdict(twin_root, twin_base),
        {"reach_check": ("reach_paths", "foreign_dirt", "seeds_for_landing")})
    root, base = build_land_clone(parent, source, stack, overlay)
    row, result = _measure(LAND_RUN, lambda: _call_land_with_verdict(root, base), landing_set=list(LANDING_SET))
    row.attribution["outcome"] = _outcome(result) if result is not None else row.attribution.get("error", "none")
    if isinstance(result, dict):
        row.attribution["refused"] = result.get("refused")
        row.attribution["detail"] = str(result.get("error") or "")[:400]
    row.attribution["profiled"] = profiled
    row.attribution["overlay"] = list(overlay)
    rows = [row]
    try:
        from coordinator_core.ops.dispatch_emit.landing_reach import foreign_dirt, reach_paths
    except ImportError:
        return rows
    reach_row, reach = _measure(REACH_CHECK, lambda: reach_paths(root, [*LANDING_SET]), landing_set=list(LANDING_SET))
    walk_row, dirt = _measure(REACH_CHECK, lambda: foreign_dirt(root, reach or [], list(LANDING_SET)))
    reach_row.process_time_ms = round(reach_row.process_time_ms + walk_row.process_time_ms, 3)
    reach_row.spawns += walk_row.spawns
    reach_row.added_git_spawns += walk_row.added_git_spawns
    reach_row.attribution["reach_paths"] = len(reach or [])
    reach_row.attribution["foreign_dirt"] = len(dirt or {})
    rows.append(reach_row)
    return rows


def _warm_imports() -> None:
    """Import the op modules before any timed bracket: the engine is warm in production, so a first-import
    is not an op's cost."""
    import importlib

    for name in ("land_run", "measured_rev", "landing_integrity", "landing_reach", "tests._review_fixture"):
        try:
            importlib.import_module(f"coordinator_core.ops.dispatch_emit.{name}")
        except ImportError:
            pass


def run(source: Path, workdir: Path, overlay=()) -> list:
    declare_benchmark_origin()
    _bind_session()
    _warm_imports()
    rows = []
    with ExitStack() as stack:
        rows.append(profile_measured_rev(source, stack, overlay))
    with ExitStack() as stack:
        rows.extend(profile_land_run(workdir, source, stack, overlay))
    return rows


def render(rows: list, bar_ms: float = BAR_MS) -> str:
    out = [f"{'axis':26} {'process_ms':>11} {'spawns':>6} {'added_git_spawns':>16}  note"]
    for r in rows:
        note = r.attribution.get("outcome") or ""
        out.append(f"{r.axis:26} {r.process_time_ms:11.1f} {r.spawns:6d} {r.added_git_spawns:16d}  "
                   f"{'OVER ' if r.process_time_ms >= bar_ms else 'ok '}{note}")
    return "\n".join(out)


def exit_code(rows: list, bar_ms: float = BAR_MS) -> int:
    """0 when the reach module is present on every land_run row and every row is under `bar_ms` with no
    added git spawns."""
    reach_present = all(r.attribution.get("reach_module_present") for r in rows if r.axis == LAND_RUN)
    return 0 if reach_present and all(r.process_time_ms < bar_ms and r.added_git_spawns == 0 for r in rows) else 1


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--source-repo", default=str(_REPO), help="repo whose HEAD the clones carry")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--bar-ms", type=float, default=BAR_MS, help="per-op process-time bar (default BAR_MS)")
    ap.add_argument("--overlay", action="append", default=[], metavar="REL_PATH",
                    help="repo-relative file copied from the source working tree into every clone (repeatable)")
    args = ap.parse_args(argv)
    if args.dir is None:
        from coordinator_core.temp_layout import make_managed_temp_dir

        workdir = make_managed_temp_dir(prefix="lgrp-")
    else:
        from coordinator_core.temp_layout import mkdtemp_in
        workdir = mkdtemp_in("lgrp-", args.dir)
    try:
        with fixture_settings_home(workdir):
            rows = run(Path(args.source_repo), workdir, tuple(args.overlay))
    finally:
        from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

        rmtree_or_raise(workdir, label="land-gate-reach")
    print(json.dumps([asdict(r) for r in rows], indent=1) if args.json else render(rows, args.bar_ms))
    return exit_code(rows, args.bar_ms)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
