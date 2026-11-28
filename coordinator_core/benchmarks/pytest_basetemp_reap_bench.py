"""Falsifier for the pytest basetemp leak: does a finished run leave a basetemp, and does the prune reap dead-pid run dirs in budget.

Default mode builds a fixture coordinator Temp (N dead-pid `<pid>-<hex>` run dirs shaped like a small
checkout, plus a live-owner, a fresh kept-marker and an unparseable survivor), runs `--passes` default
`prune_claude_temp.prune()` passes and reports per pass `process_time_ms`, `spawns`, `listings`,
`walks`, `rmtrees`, `deletes`, `run_dirs_remaining` and whether the top-level `.prune-stamp` was written.
It then runs one tiny default pytest session with Temp redirected into the fixture and reports whether
its minted basetemp survives sessionfinish. Wall clock is never recorded. Exit 0 only when every
`expected_when_true` check holds.

`--spike` is the read-only live census (one listdir per pytest dir; per run dir one lstat, one kept-marker
probe and one pid probe; never a walk). `--drain-rate` measures entries deleted per budgeted pass.

    python coordinator_core/benchmarks/pytest_basetemp_reap_bench.py [--dirs 40] [--passes 4] [--json]
    python coordinator_core/benchmarks/pytest_basetemp_reap_bench.py --spike [--json]
    python coordinator_core/benchmarks/pytest_basetemp_reap_bench.py --drain-rate [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

FIXTURE_REPO = "reapbench"
DEAD_PID_FLOOR = 3_000_000
DEFAULT_TARGET = "coordinator_core/tests/test_source_free_traceback.py"
SESSION_ROOTDIR = "coordinator_core"
PARSEABLE_SURVIVORS = 2
FLEET_CHILDREN = 3000
AGE_BINS = (("lt1h", 3600.0), ("lt1d", 86400.0), ("lt7d", 7 * 86400.0), ("ge7d", float("inf")))
SPIKE_SAMPLE = 40
ENTRIES_PER_RUN_DIR = 2200


class Meter:
    """Counts spawns, listings, walks, rmtrees and entry deletions while active; restores every seam on exit."""

    def __init__(self) -> None:
        self.counts = Counter()

    def __enter__(self) -> "Meter":
        self._saved = []
        meter = self

        def wrap(owner, name, key) -> None:
            fn = getattr(owner, name)
            self._saved.append((owner, name, fn))

            def counted(*a, **k) -> object:
                meter.counts[key] += 1
                return fn(*a, **k)

            setattr(owner, name, counted)

        init = subprocess.Popen.__init__
        self._saved.append((subprocess.Popen, "__init__", init))

        def counted_init(proc, *a, **k) -> None:
            meter.counts["spawns"] += 1
            return init(proc, *a, **k)

        subprocess.Popen.__init__ = counted_init
        for name in ("scandir", "listdir"):
            wrap(os, name, "listings")
        wrap(os, "walk", "walks")
        wrap(shutil, "rmtree", "rmtrees")
        for name in ("unlink", "remove", "rmdir"):
            wrap(os, name, "deletes")
        return self

    def __exit__(self, *exc) -> None:
        for owner, name, fn in reversed(self._saved):
            setattr(owner, name, fn)


def _no_console() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _age_bin(age: float) -> str:
    return next(label for label, limit in AGE_BINS if age < limit)


def _dead_pids(count: int, pid_alive) -> "list[int]":
    out: "list[int]" = []
    pid = DEAD_PID_FLOOR
    while len(out) < count:
        if not pid_alive(pid):
            out.append(pid)
        pid += 1
    return out


def _stamp_age(path: Path, now: float) -> "Optional[float]":
    try:
        return round((now - path.stat().st_mtime) / 3600.0, 2)
    except OSError:
        return None


def _warm_state(pid_alive) -> dict:
    from coordinator_core.warm import breadcrumb

    record = breadcrumb.read_breadcrumb()
    if record is None:
        return {"breadcrumb": False, "alive": False}
    pid = record.get("pid")
    return {
        "breadcrumb": True,
        "pid": pid,
        "alive": bool(isinstance(pid, int) and pid_alive(pid)),
        "started_at": record.get("started_at"),
    }


def sibling_census() -> dict:
    """One listdir per `<root>/<repo>`: the non-`pytest` children that hold the per-repo bulk, by name, plus top-level name shapes."""
    from coordinator_core.temp_layout import pytest_sweep_roots

    shapes: Counter = Counter()
    siblings: Counter = Counter()
    for root in pytest_sweep_roots():
        for repo in _names(root):
            hashed = len(repo) == 40 and all(c in "0123456789abcdef" for c in repo)
            shapes["hash40" if hashed else "dot" if repo.startswith(".") else "named"] += 1
            for child in _names(root / repo):
                if child != "pytest":
                    siblings[child if not child.endswith(".log") else "*.log"] += 1
    return {"top_level_shapes": dict(shapes), "repo_children_other_than_pytest": dict(siblings.most_common(12))}


def spike(sample: int = SPIKE_SAMPLE, now: "Optional[float]" = None) -> dict:
    """Read-only census of every `pytest_sweep_roots()/<repo>/pytest` dir: bucket by owner state and age."""
    from coordinator_core.group_em.session_registry import pid_alive
    from coordinator_core.temp_layout import (
        FLEET_DIRNAME,
        PYTEST_KEPT_MARKER,
        fleet_temp_root,
        parse_pytest_run_dir,
        pytest_sweep_roots,
    )

    now = time.time() if now is None else now
    alive_cache: "dict[int, bool]" = {}

    def alive(pid: int) -> bool:
        if pid not in alive_cache:
            alive_cache[pid] = pid_alive(pid)
        return alive_cache[pid]

    buckets: "Counter[tuple[str, str]]" = Counter()
    per_repo: "dict[str, Counter]" = {}
    oldest = 0.0
    unmarked_dead: "list[Path]" = []
    roots_seen = []
    for root in pytest_sweep_roots():
        try:
            repos = sorted(os.listdir(root))
        except OSError:
            continue
        roots_seen.append(str(root))
        for repo in repos:
            pytest_dir = root / repo / "pytest"
            try:
                runs = os.listdir(pytest_dir)
            except OSError:
                continue
            tally = per_repo.setdefault(f"{root}/{repo}", Counter())
            for name in runs:
                run = pytest_dir / name
                pid = parse_pytest_run_dir(name)
                try:
                    age = now - os.lstat(run).st_mtime
                except OSError:
                    continue
                if pid is None:
                    state = "reap_dir" if name == ".reap" else "unparseable"
                elif alive(pid):
                    state = "live"
                elif os.path.exists(run / PYTEST_KEPT_MARKER):
                    state = "dead_kept"
                else:
                    state = "dead_unmarked"
                    if repo != FLEET_DIRNAME:
                        unmarked_dead.append(run)
                buckets[(state, _age_bin(age))] += 1
                tally[state] += 1
                oldest = max(oldest, age)
    step = max(1, len(unmarked_dead) // max(1, sample))
    picked = unmarked_dead[::step][:sample]
    entry_counts: "list[int]" = []
    top_names: Counter = Counter()
    for run in picked:
        try:
            names = os.listdir(run)
        except OSError:
            continue
        entry_counts.append(len(names))
        top_names.update(n.rstrip("0123456789") or n for n in names)
    entry_counts.sort()
    top_stamp = fleet_temp_root().parent / ".prune-stamp"
    from coordinator_core.ops import prune_claude_temp

    root_stamps = {
        str(r): ("absent" if not r.is_dir() else _stamp_age(r / prune_claude_temp.STAMP_NAME, now))
        for r, _depth, _days in prune_claude_temp.default_roots(str(_REPO))
    }
    totals: "Counter[str]" = Counter()
    for (state, _), n in buckets.items():
        totals[state] += n
    return {
        "roots": roots_seen,
        "totals": dict(totals),
        "by_state_age": {f"{s}/{a}": n for (s, a), n in sorted(buckets.items())},
        "oldest_age_days": round(oldest / 86400.0, 1),
        "per_repo_dirs": {k: dict(v) for k, v in per_repo.items() if sum(v.values())},
        "sample_dead_unmarked": {
            "n": len(entry_counts),
            "top_level_entries_min_median_max": (
                [entry_counts[0], entry_counts[len(entry_counts) // 2], entry_counts[-1]] if entry_counts else []
            ),
            "top_level_name_stems": dict(top_names.most_common(8)),
        },
        "top_level_stamp_age_hours": _stamp_age(top_stamp, now),
        "root_stamp_age_hours": root_stamps,
        "system_temp_top_level_entries": len(_names(Path(tempfile.gettempdir()))),
        "warm_supervisor": _warm_state(pid_alive),
        "per_repo_bulk": sibling_census(),
    }


def _populate_run_dir(run: Path, files: int, mtime: float) -> None:
    for sub in (".git/objects/ab", ".git/refs", "state", ".coordinator-local", "test_case_0/repo"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    (run / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    subs = (".git/objects/ab", "state", ".coordinator-local", "test_case_0/repo")
    for i in range(max(0, files - 1)):
        (run / subs[i % len(subs)] / f"f{i}.txt").write_text("x" * 64, encoding="utf-8")
    os.utime(run, (mtime, mtime))


def build_fixture(temp: Path, dirs: int, files: int, now: float, fleet_children: int) -> dict:
    """`dirs` dead-pid run dirs plus survivors under `<temp>/coordinator/<FIXTURE_REPO>/pytest`, and `fleet_children`
    fresh `_fleet` units that spend the shared budget ahead of every later root; returns survivor paths."""
    from coordinator_core.group_em.session_registry import pid_alive
    from coordinator_core.temp_layout import PYTEST_KEPT_MARKER

    pytest_dir = temp / "coordinator" / FIXTURE_REPO / "pytest"
    pytest_dir.mkdir(parents=True)
    old = now - 48 * 3600
    for i, pid in enumerate(_dead_pids(dirs, pid_alive)):
        _populate_run_dir(pytest_dir / f"{pid}-{i:06x}", files, old)
    survivors = {
        "live_own_pid": pytest_dir / f"{os.getpid()}-aaaaaa",
        "kept_fresh": pytest_dir / f"{_dead_pids(dirs + 1, pid_alive)[-1]}-bbbbbb",
        "unparseable": pytest_dir / "not-a-run-dir",
    }
    for path in survivors.values():
        _populate_run_dir(path, 4, old)
    (survivors["kept_fresh"] / PYTEST_KEPT_MARKER).touch()
    fleet = temp / "coordinator" / "_fleet"
    for i in range(fleet_children):
        child = fleet / f"live-unit-{i}" / "sub"
        child.mkdir(parents=True)
        (child / "f.txt").write_text("x", encoding="utf-8")
    return {k: str(v) for k, v in survivors.items()}


def _run_dirs(pytest_dir: Path) -> "list[str]":
    from coordinator_core.temp_layout import parse_pytest_run_dir

    try:
        return [n for n in os.listdir(pytest_dir) if parse_pytest_run_dir(n) is not None]
    except OSError:
        return []


def _prune_pass(repo: Path, session_id: str) -> dict:
    from coordinator_core.ops import prune_claude_temp

    with Meter() as meter:
        cpu0 = time.process_time()
        removed = prune_claude_temp.prune(session_id, repo_root=str(repo))
        cpu = time.process_time() - cpu0
    c = meter.counts
    return {
        "process_time_ms": round(cpu * 1000.0, 3),
        "removed": removed,
        "spawns": c["spawns"],
        "listings": c["listings"],
        "walks": c["walks"],
        "rmtrees": c["rmtrees"],
        "deletes": c["deletes"],
    }


def _redirected(temp: Path) -> "Callable[[], None]":
    saved = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")}
    saved_tempdir = tempfile.tempdir
    for key in saved:
        os.environ[key] = str(temp)
    tempfile.tempdir = str(temp)

    def restore() -> None:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tempfile.tempdir = saved_tempdir

    return restore


def run_session(temp: Path, target: str) -> dict:
    """One default `python -m pytest <target>` over the real repo with Temp redirected into `temp`."""
    from coordinator_core import temp_layout

    env = dict(os.environ)
    for key in ("TEMP", "TMP", "TMPDIR"):
        env[key] = str(temp)
    restore = _redirected(temp)
    try:
        root = temp_layout.pytest_basetemp_root(_REPO / SESSION_ROOTDIR)
    finally:
        restore()
    before = set(_names(root))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
        env=env, cwd=str(_REPO), capture_output=True, text=True, creationflags=_no_console(),
    )
    left = sorted(set(_names(root)) - before)
    return {
        "target": target,
        "exit_code": proc.returncode,
        "basetemp_root": str(root),
        "basetemps_left": left,
        "basetemp_survives": bool(left),
        "tail": proc.stdout.strip().splitlines()[-1:] if proc.stdout else [],
    }


def _names(path: Path) -> "list[str]":
    try:
        return os.listdir(path)
    except OSError:
        return []


def bench(dirs: int, files: int, passes: int, target: Optional[str], fleet_children: int = FLEET_CHILDREN) -> dict:
    from coordinator_core import temp_layout
    from coordinator_core.ops import prune_claude_temp

    with temp_layout.managed_temp_dir("pytest-basetemp-reap-bench", _REPO) as work:
        temp = work / "temp"
        repo = work / FIXTURE_REPO
        repo.mkdir()
        now = time.time()
        restore = _redirected(temp)
        try:
            temp.mkdir()
            survivors = build_fixture(temp, dirs, files, now, fleet_children)
            pytest_dir = temp / "coordinator" / FIXTURE_REPO / "pytest"
            stamp = prune_claude_temp._top_level_stamp_root() / prune_claude_temp.STAMP_NAME
            rows = []
            for n in range(1, passes + 1):
                row = _prune_pass(repo, f"reap-bench-{n}")
                row["pass"] = n
                row["run_dirs_remaining"] = len(_run_dirs(pytest_dir))
                row["reap_entries"] = len(_names(pytest_dir / ".reap"))
                row["top_level_stamp_written"] = stamp.exists()
                rows.append(row)
            survivors_ok = all(os.path.lexists(p) for p in survivors.values())
        finally:
            restore()
        session = run_session(temp, target) if target else None
    budget_ms = prune_claude_temp.BUDGET_SECONDS * 1000.0
    checks = {
        "dead_dirs_drained": rows[-1]["run_dirs_remaining"] == PARSEABLE_SURVIVORS,
        "each_pass_under_budget": all(r["process_time_ms"] < budget_ms for r in rows),
        "no_recursive_walk_in_pass": all(r["walks"] == 0 and r["rmtrees"] == 0 for r in rows),
        "top_level_stamp_written": rows[-1]["top_level_stamp_written"],
        "survivors_intact": survivors_ok,
        "finished_run_leaves_no_basetemp": session is not None and session["exit_code"] == 0 and not session["basetemp_survives"],
    }
    return {
        "dirs": dirs, "files_per_dir": files, "budget_ms": budget_ms,
        "passes": rows, "survivors_intact": survivors_ok, "session": session,
        "checks": checks, "green": all(checks.values()),
    }


def drain_rate(entries: int, budget_seconds: float) -> dict:
    """Entries a bottom-up one-entry-at-a-time delete clears per `budget_seconds` of process time (cache-hot fixture)."""
    from coordinator_core import temp_layout

    with temp_layout.managed_temp_dir("pytest-basetemp-drain-rate", _REPO) as work:
        run = work / "run"
        dirs_n = int(entries * 9.8 / 22.1)
        files_n = entries - dirs_n
        leaves = [run / f"d{i // 40}" / f"e{i}" for i in range(dirs_n)]
        for leaf in leaves:
            leaf.mkdir(parents=True, exist_ok=True)
        for i in range(files_n):
            (leaves[i % len(leaves)] / f"f{i}.txt").write_text("x" * 64, encoding="utf-8")
        order = [Path(dp) / n for dp, dn, fn in os.walk(run, topdown=False) for n in (*fn, *dn)]
        cpu0 = time.process_time()
        done = 0
        for path in order:
            if time.process_time() - cpu0 >= budget_seconds:
                break
            try:
                os.rmdir(path) if path.is_dir() else os.unlink(path)
            except OSError:
                pass
            done += 1
    return {
        "fixture_entries": len(order),
        "budget_ms": budget_seconds * 1000.0,
        "entries_per_pass": done,
        "machine_a_entries": 22_100_000,
        "passes_to_empty_machine_a": round(22_100_000 / done) if done else None,
        "entries_per_run_dir": ENTRIES_PER_RUN_DIR,
    }


def main(argv: "Optional[list[str]]" = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dirs", type=int, default=40)
    p.add_argument("--files", type=int, default=24)
    p.add_argument("--passes", type=int, default=4)
    p.add_argument("--fleet-children", type=int, default=FLEET_CHILDREN)
    p.add_argument("--target", default=DEFAULT_TARGET, help="tiny pytest target; empty skips the session leg")
    p.add_argument("--spike", action="store_true")
    p.add_argument("--drain-rate", action="store_true")
    p.add_argument("--entries", type=int, default=6000)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.spike:
        result = spike()
    elif args.drain_rate:
        from coordinator_core.ops import prune_claude_temp

        result = drain_rate(args.entries, prune_claude_temp.BUDGET_SECONDS)
    else:
        result = bench(args.dirs, args.files, args.passes, args.target or None, args.fleet_children)
    if args.json or args.spike or args.drain_rate:
        print(json.dumps(result, indent=2, default=str))
    else:
        for r in result["passes"]:
            print(
                f"pass={r['pass']} process_time_ms={r['process_time_ms']} spawns={r['spawns']} "
                f"walks={r['walks']} rmtrees={r['rmtrees']} deletes={r['deletes']} "
                f"run_dirs_remaining={r['run_dirs_remaining']} reap_entries={r['reap_entries']} "
                f"top_level_stamp_written={r['top_level_stamp_written']}"
            )
        s = result["session"]
        if s:
            print(f"session exit={s['exit_code']} basetemp_survives={s['basetemp_survives']} left={s['basetemps_left']}")
        print(f"checks={result['checks']}")
    return 0 if args.spike or args.drain_rate or result["green"] else 1


if __name__ == "__main__":
    sys.exit(main())
