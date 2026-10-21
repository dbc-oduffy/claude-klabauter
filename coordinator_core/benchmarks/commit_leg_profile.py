"""Phase-timed profile of one `commit_paths` call: where does one commit's process time go.

Run it against a FRESH clone, one clone per run (the commit consumes it):

    python -I coordinator_core/benchmarks/commit_leg_profile.py <clone> {phased|cprofile} \
        --paths N --sign {config|off}

`<clone>` comes from `isolated_clone.mkdtemp_for_clone`. `--sign off` writes
`commit.gpgsign=false` into the clone's own `.git/config` (`_gpgsign_enabled` reads config
files, so an environment override would not reach it); `--sign config` leaves the box's
signing configuration as git resolves it.

Output is one JSON object: `mode, sign, paths, head, committed, sha, parent_ms, children_ms,
spawns, phases`. `parent_ms` brackets the `commit_paths` call in this process;
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
_IMPORT_PHASES = (
    "coordinator_core.git.action_guard",
    "coordinator_core.git.rollback_check",
    "coordinator_core.git.sshsig",
)

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


def _instrument(acc: Dict[str, float], counts: Dict[str, int]) -> Callable[[], None]:
    """Wrap every available phase target in a process_time bracket; return the restorer."""
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
            @functools.wraps(fn)
            def wrapper(*a, **k):
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


def build_report(repo, mode: str = "phased", n_paths: int = 30, sign: str = "config") -> dict:
    """Make one commit of `n_paths` touched tracked files in `repo` and return the report."""
    if mode not in ("phased", "cprofile"):
        raise ValueError(f"mode must be phased or cprofile, got {mode!r}")
    if sign not in ("config", "off"):
        raise ValueError(f"sign must be config or off, got {sign!r}")
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
    restore = _instrument(acc, counts)

    del _SPAWNS[:]
    meter = _ChildMeter()
    profiler = None
    if mode == "cprofile":
        import cProfile
        profiler = cProfile.Profile()
    sha: Optional[str] = None
    error: Optional[str] = None
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
        restore()

    report = {
        "mode": mode,
        "sign": sign,
        "paths": n_paths,
        "head": head,
        "committed": sha is not None,
        "sha": sha,
        "parent_ms": _ms(parent),
        "children_ms": children_ms,
        "spawns": len(_SPAWNS),
        "phases": {k: _ms(v) for k, v in sorted(acc.items())},
    }
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
    args = ap.parse_args(argv)

    clone = Path(args.clone).resolve()
    sys.path.insert(0, str(clone))
    os.chdir(clone)
    from coordinator_core.benchmarks import declare_benchmark_origin
    declare_benchmark_origin()
    print(json.dumps(build_report(clone, args.mode, args.paths, args.sign), indent=1))
    return 0


if __name__ == "__main__":
    _install_spawn_counter()
    raise SystemExit(main())
