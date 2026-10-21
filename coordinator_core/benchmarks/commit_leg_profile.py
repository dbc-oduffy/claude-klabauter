"""Phase-timed profile of one `commit_paths` call: where does one commit's process time go.

Run it against a FRESH clone, one clone per run (the commit consumes it):

    python -I coordinator_core/benchmarks/commit_leg_profile.py <clone> {phased|cprofile} \
        --paths N --sign {config|off}

`<clone>` comes from `isolated_clone.mkdtemp_for_clone`. `--sign off` writes
`commit.gpgsign=false` into the clone's own `.git/config` (`_gpgsign_enabled` reads config
files, so an environment override would not reach it); `--sign config` leaves the box's
signing configuration as git resolves it.

Output is one JSON object: `mode, sign, paths, head, committed, sha, parent_ms, children_ms,
spawns, phases, per_commit`. `--sub-phases` adds `sub_phases` and `sub_counts` (nested
brackets, never summed into `parent_ms`); `--commits K` runs K sequential commits and
`per_commit` carries one entry each, the base keys staying the first commit's. `parent_ms` brackets the `commit_paths` call in this process;
`children_ms` is the process time of every descendant it spawned. Phases are
`time.process_time` brackets, never cProfile (DR-368: cProfile inflates with call count);
`cprofile` mode adds a `calls` key and is for call counts only.
"""
import argparse
import collections
import functools
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

# (module, attribute) pairs bracketed as phases. Attributes absent from the engine in hand
# are skipped, so the harness keeps running across a refactor of commit.py.
_PHASE_TARGETS = (
    ("coordinator_core.git.commit", "_attach_session_id_trailer"),
    ("coordinator_core.git.checkpoint_guard", "checkpoint_refusal"),
    ("coordinator_core.git.action_guard", "assert_pathspec_shape_permitted"),
    ("coordinator_core.git.commit", "parse_index_identity"),
    ("coordinator_core.git.commit", "_worktree_blob"),
    ("coordinator_core.git.commit", "head_sha"),
    ("coordinator_core.git.commit", "read_tree_spine"),
    ("coordinator_core.git.commit", "_rewrite_head_spine"),
    ("coordinator_core.git.commit", "_identity"),
    ("coordinator_core.git.commit", "_gpgsign_enabled"),
    ("coordinator_core.git.commit", "_sign_commit_tree"),
    ("coordinator_core.git.commit", "flush_object_batch"),
    ("coordinator_core.git.commit", "cas_ref"),
    ("coordinator_core.git.index_write", "splice_index"),
)

# Phase labels `--memoise` accepts: read-only calls whose repeat result is the first result
# within one commit. Anything else (writers, ref movers, signers) exits 2.
_MEMOISE_ALLOWLIST = (
    "commit._identity",
    "commit._gpgsign_enabled",
    "commit.head_sha",
    "commit.read_tree_spine",
    "commit.parse_index_identity",
    "checkpoint_guard.checkpoint_refusal",
    "action_guard.assert_pathspec_shape_permitted",
)
_IMPORT_PHASES = (
    "coordinator_core.git.action_guard",
    "coordinator_core.git.rollback_check",
    "coordinator_core.git.sshsig",
)

# `--sub-phases` targets: functions that nest inside the top-level phases. Bracketed at the
# outermost frame only, so a recursive chain (OFS_DELTA) is counted once.
_SUB_TARGETS = (
    ("coordinator_core.git.git_objects", "_parse_pack_index_uncached"),
    ("coordinator_core.git.git_objects", "_pack_index_find"),
    ("coordinator_core.git.git_objects", "_read_pack_object_at"),
    ("coordinator_core.git.git_objects", "_zlib_decompress_bounded"),
    ("coordinator_core.git.git_objects", "_apply_git_delta"),
    ("coordinator_core.git.git_objects", "_read_loose_object"),
    ("coordinator_core.git.git_state", "_parse_tree_entries"),
)
# Modules `commit._attach_session_id_trailer` imports lazily: the import is bracketed through a
# `builtins.__import__` hook, and the named function is wrapped once the module is loaded, so
# pre-importing them here never warms the bracket.
_LAZY_SUB_TARGETS = {
    "coordinator_core.session.core": ("resolve_session_id",),
    "coordinator_core.git.commit_trailers": ("format_trailers_in_process",),
}

_SPAWNS: List[object] = []
_HOOKED = False


def _install_spawn_counter() -> None:
    """Count `subprocess.Popen` constructions; idempotent, must run before engine import."""
    global _HOOKED
    if _HOOKED:
        return
    original = subprocess.Popen.__init__

    def _init(self, args, *a, **k):
        _SPAWNS.append(list(args) if isinstance(args, (list, tuple)) else args)
        return original(self, args, *a, **k)

    subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
    _HOOKED = True


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


def _git(repo: Path, *args: str) -> str:
    """Setup-time git read/write; runs before the spawn counter is reset."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _pick_paths(repo: Path, n: int) -> List[str]:
    tracked = _git(repo, "ls-files", "-z").split("\0")
    chosen = [p for p in tracked if p and not p.startswith("scratch/") and p.endswith((".py", ".md"))]
    if len(chosen) < n:
        raise SystemExit(f"clone tracks {len(chosen)} editable files, need {n}")
    return chosen[:n]


def _touch(repo: Path, paths: List[str]) -> None:
    for rel in paths:
        with open(repo / rel, "ab") as fh:
            fh.write(b"\n")


class _ChildMeter:
    """Process time of every descendant spawned inside the bracket, per OS.

    posix: RUSAGE_CHILDREN delta (reaped children only, which `run_git` always waits on).
    Windows: the job-object tree total from `process_time.LiveTreeAccountant` minus this
    process's own CPU, so the figure is descendants only.
    """

    def __init__(self) -> None:
        self._win = None
        if os.name == "nt":
            from coordinator_core.benchmarks.process_time import LiveTreeAccountant
            self._win = LiveTreeAccountant(os.getpid())

    def start(self) -> None:
        self._own0 = time.process_time()
        if self._win is not None:
            self._tree0 = self._win.snapshot()["process_time_ms"]
        else:
            import resource
            r = resource.getrusage(resource.RUSAGE_CHILDREN)
            self._child0 = r.ru_utime + r.ru_stime

    def stop_ms(self) -> float:
        if self._win is not None:
            tree = self._win.snapshot()["process_time_ms"] - self._tree0
            own = (time.process_time() - self._own0) * 1000.0
            self._win.close()
            return round(max(tree - own, 0.0), 3)
        import resource
        r = resource.getrusage(resource.RUSAGE_CHILDREN)
        return _ms((r.ru_utime + r.ru_stime) - self._child0)


class _SubPhases:
    """Outermost-frame `process_time` brackets for the `--sub-phases` targets."""

    def __init__(self) -> None:
        self.acc: Dict[str, float] = {}
        self.counts: Dict[str, int] = {}
        self._depth: Dict[str, int] = collections.defaultdict(int)
        self._undo: List[tuple] = []
        self._orig_import = None

    @staticmethod
    def _label(modname: str, attr: str) -> str:
        return f"{modname.rsplit('.', 1)[-1]}.{attr}"

    def _enter(self, label: str) -> Optional[float]:
        self.acc.setdefault(label, 0.0)
        self.counts.setdefault(label, 0)
        self._depth[label] += 1
        return time.process_time() if self._depth[label] == 1 else None

    def _leave(self, label: str, started: Optional[float]) -> None:
        self._depth[label] -= 1
        if started is not None:
            self.acc[label] += time.process_time() - started
            self.counts[label] += 1

    def _wrap(self, mod, attr: str) -> None:
        fn = getattr(mod, attr, None)
        if fn is None or getattr(fn, "_sub_phase_wrapped", False):
            return
        label = self._label(mod.__name__, attr)
        self.acc.setdefault(label, 0.0)
        self.counts.setdefault(label, 0)
        outer = self

        @functools.wraps(fn)
        def wrapper(*a, **k):
            started = outer._enter(label)
            try:
                return fn(*a, **k)
            finally:
                outer._leave(label, started)

        wrapper._sub_phase_wrapped = True  # type: ignore[attr-defined]
        setattr(mod, attr, wrapper)
        self._undo.append((mod, attr, fn))

    def install(self) -> None:
        for modname, attr in _SUB_TARGETS:
            try:
                self._wrap(importlib.import_module(modname), attr)
            except ImportError:
                continue
        for modname, attrs in _LAZY_SUB_TARGETS.items():
            self.acc.setdefault(f"import.{modname}", 0.0)
            self.counts.setdefault(f"import.{modname}", 0)
            if modname in sys.modules:
                for attr in attrs:
                    self._wrap(sys.modules[modname], attr)
        import builtins
        self._orig_import = builtins.__import__
        outer = self

        def hooked(name, globals=None, locals=None, fromlist=(), level=0):
            hit = None
            if level == 0:
                if name in _LAZY_SUB_TARGETS:
                    hit = name
                else:
                    for f in fromlist or ():
                        if f"{name}.{f}" in _LAZY_SUB_TARGETS:
                            hit = f"{name}.{f}"
            if hit is None:
                return outer._orig_import(name, globals, locals, fromlist, level)
            label = f"import.{hit}"
            started = outer._enter(label)
            try:
                return outer._orig_import(name, globals, locals, fromlist, level)
            finally:
                outer._leave(label, started)
                if hit in sys.modules:
                    for attr in _LAZY_SUB_TARGETS[hit]:
                        outer._wrap(sys.modules[hit], attr)

        builtins.__import__ = hooked

    def take(self) -> tuple:
        """Return `(sub_phases, sub_counts)` since the last take, then zero both."""
        phases = {k: _ms(v) for k, v in sorted(self.acc.items())}
        counts = dict(sorted(self.counts.items()))
        for k in self.acc:
            self.acc[k] = 0.0
            self.counts[k] = 0
        return phases, counts

    def restore(self) -> None:
        import builtins
        if self._orig_import is not None:
            builtins.__import__ = self._orig_import
        for mod, attr, fn in self._undo:
            setattr(mod, attr, fn)


def _instrument(acc: Dict[str, float], counts: Dict[str, int],
                memoise: Optional[str] = None,
                caches: Optional[List[dict]] = None) -> Callable[[], None]:
    """Wrap every available phase target in a process_time bracket; return the restorer.

    `memoise` is the removal-check variant "cache on the first call inside the bracket and
    serve later calls": the first call per argument repr is timed as usual, repeats return the
    cached result untimed, so the drop in `parent_ms` against an unmemoised run is the repeat
    calls' share of that phase.
    """
    undo = []
    for modname, attr in _PHASE_TARGETS:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        fn = getattr(mod, attr, None)
        if fn is None:
            continue
        label = f"{modname.rsplit('.', 1)[-1]}.{attr}"

        def make(fn=fn, label=label):
            cache: Dict[str, object] = {}
            if caches is not None:
                caches.append(cache)

            @functools.wraps(fn)
            def wrapper(*a, **k):
                if label == memoise:
                    key = repr((a, sorted(k.items())))
                    if key in cache:
                        counts[label + ".served"] += 1
                        return cache[key]
                    t = time.process_time()
                    try:
                        cache[key] = fn(*a, **k)
                        return cache[key]
                    finally:
                        acc[label] += time.process_time() - t
                        counts[label] += 1
                t = time.process_time()
                try:
                    return fn(*a, **k)
                finally:
                    acc[label] += time.process_time() - t
                    counts[label] += 1
            return wrapper

        setattr(mod, attr, make())
        undo.append((mod, attr, fn))

    def restore() -> None:
        for mod, attr, fn in undo:
            setattr(mod, attr, fn)

    return restore


def _commit(repo: Path, paths: List[str], message: str):
    """The one committing call: `commit_paths` exactly as an engine caller makes it."""
    from functools import partial
    from coordinator_core.git.commit import commit_paths, hash_worktree_blobs_via_spawn
    return commit_paths(
        repo, paths, message,
        blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=repo),
    )


def _bracket_callers(repo: Path, paths: List[str]) -> Dict[str, float]:
    """Time one `compose_message` and one `apply_missing_trailers`, shaped as the pipeline probe."""
    from coordinator_core.git.commit_trailers import apply_missing_trailers
    from coordinator_core.ops.ceremony.commit_message import compose_message
    t = time.process_time()
    message = compose_message(subject="commit-leg-profile")
    compose = time.process_time() - t
    t = time.process_time()
    apply_missing_trailers(message, repo, paths)
    trailers = time.process_time() - t
    return {"caller.compose_message": _ms(compose),
            "caller.apply_missing_trailers": _ms(trailers)}


def build_report(repo, mode: str = "phased", n_paths: int = 30, sign: str = "config",
                 memoise: Optional[str] = None, with_caller: bool = False,
                 sub_phases: bool = False, commits: int = 1) -> dict:
    """Make `commits` sequential commits of `n_paths` touched tracked files in `repo`.

    `memoise` (an allowlisted phase label) adds `memoised`; `with_caller` adds `caller_phases`;
    `sub_phases` adds `sub_phases` and `sub_counts` (nested brackets, never summed into
    `parent_ms`). `per_commit` lists one `{committed, sha, parent_ms, children_ms, spawns,
    phases[, sub_phases, sub_counts]}` per commit. The ten base keys describe the first
    commit and keep their meaning; commit 2 onward is the warm figure.
    """
    if commits < 1:
        raise ValueError(f"commits must be >= 1, got {commits!r}")
    if mode not in ("phased", "cprofile"):
        raise ValueError(f"mode must be phased or cprofile, got {mode!r}")
    if sign not in ("config", "off"):
        raise ValueError(f"sign must be config or off, got {sign!r}")
    if memoise is not None and memoise not in _MEMOISE_ALLOWLIST:
        raise ValueError(
            f"memoise label {memoise!r} not allowed; read-only allowlist: "
            + ", ".join(_MEMOISE_ALLOWLIST))
    repo = Path(repo)
    _install_spawn_counter()
    if sign == "off":
        _git(repo, "config", "commit.gpgsign", "false")
    paths = _pick_paths(repo, n_paths)
    _touch(repo, paths)
    head = _git(repo, "rev-parse", "HEAD").strip()

    acc: Dict[str, float] = collections.defaultdict(float)
    counts: Dict[str, int] = collections.defaultdict(int)
    for modname in _IMPORT_PHASES:
        t = time.process_time()
        importlib.import_module(modname)
        acc[f"import.{modname.rsplit('.', 1)[-1]}"] += time.process_time() - t
    caller_phases = _bracket_callers(repo, paths) if with_caller else None
    memo_caches: List[dict] = []
    restore = _instrument(acc, counts, memoise, memo_caches)
    subs = _SubPhases() if sub_phases else None
    if subs is not None:
        subs.install()

    profiler = None
    if mode == "cprofile":
        import cProfile
        profiler = cProfile.Profile()
    per_commit: List[dict] = []
    memo_served = 0
    try:
        for index in range(commits):
            if index:
                _touch(repo, paths)
            for cache in memo_caches:
                cache.clear()
            del _SPAWNS[:]
            meter = _ChildMeter()
            sha = None
            error = None
            meter.start()
            t0 = time.process_time()
            try:
                if profiler is not None:
                    profiler.enable()
                try:
                    sha = _commit(repo, paths, "commit-leg-profile").sha
                except Exception as exc:  # noqa: BLE001 -- the report carries the failure
                    error = repr(exc)
                finally:
                    if profiler is not None:
                        profiler.disable()
            finally:
                parent = time.process_time() - t0
                children_ms = meter.stop_ms()
            entry = {
                "committed": sha is not None,
                "sha": sha,
                "parent_ms": _ms(parent),
                "children_ms": children_ms,
                "spawns": len(_SPAWNS),
                "phases": {k: _ms(v) for k, v in sorted(acc.items())},
            }
            if subs is not None:
                entry["sub_phases"], entry["sub_counts"] = subs.take()
            if error is not None:
                entry["error"] = error
            per_commit.append(entry)
            if memoise is not None:
                memo_served += counts.get(memoise + ".served", 0)
                counts[memoise + ".served"] = 0
            acc.clear()
            if error is not None:
                break
    finally:
        restore()
        if subs is not None:
            subs.restore()

    first = per_commit[0]
    error = first.get("error")
    report = {
        "mode": mode,
        "sign": sign,
        "paths": n_paths,
        "head": head,
        "committed": first["committed"],
        "sha": first["sha"],
        "parent_ms": first["parent_ms"],
        "children_ms": first["children_ms"],
        "spawns": first["spawns"],
        "phases": first["phases"],
        "per_commit": per_commit,
    }
    if subs is not None:
        report["sub_phases"] = first["sub_phases"]
        report["sub_counts"] = first["sub_counts"]
    if memoise is not None:
        report["memoised"] = memoise
        report["memoise_served"] = memo_served
    if caller_phases is not None:
        report["caller_phases"] = caller_phases
    if error is not None:
        report["error"] = error
    if profiler is not None:
        import pstats
        stats = pstats.Stats(profiler)
        top = sorted(stats.stats.items(), key=lambda kv: kv[1][1], reverse=True)[:25]
        report["calls"] = {f"{f[0]}:{f[2]}": v[1] for f, v in top}
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clone")
    ap.add_argument("mode", choices=("phased", "cprofile"))
    ap.add_argument("--paths", type=int, default=30)
    ap.add_argument("--sign", choices=("config", "off"), default="config")
    ap.add_argument("--memoise", metavar="LABEL", default=None)
    ap.add_argument("--with-caller", action="store_true")
    ap.add_argument("--sub-phases", action="store_true")
    ap.add_argument("--commits", type=int, default=1)
    args = ap.parse_args(argv)
    if args.commits < 1:
        ap.error("--commits must be >= 1")
    if args.memoise is not None and args.memoise not in _MEMOISE_ALLOWLIST:
        ap.error(f"--memoise {args.memoise!r} not allowed; read-only allowlist: "
                 + ", ".join(_MEMOISE_ALLOWLIST))

    clone = Path(args.clone).resolve()
    sys.path.insert(0, str(clone))
    os.chdir(clone)
    from coordinator_core.benchmarks import declare_benchmark_origin
    declare_benchmark_origin()
    print(json.dumps(build_report(clone, args.mode, args.paths, args.sign,
                                    args.memoise, args.with_caller, args.sub_phases,
                                    args.commits), indent=1))
    return 0


if __name__ == "__main__":
    _install_spawn_counter()
    raise SystemExit(main())
