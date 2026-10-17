"""Interleaved A/B driver for the `dispatch.terminal_commit` full path: a baseline sha against this tree.

    python -m coordinator_core.benchmarks.terminal_commit_ab --baseline <sha> [--rounds 3]
        [--out-dir <dir>] [--count-replaces] [--asyncio-importers]

Answers one question: is B (this working tree) cheaper than A (the baseline sha) on this box?
Each side is measured with the standing gate's own method (`batched_process_time_ms`, k and
samples imported from `test_terminal_commit_process_budget`, net = min(full) - min(setup-only)),
and A and B alternate in a fresh random order every round so peer load lands on both.

`--count-replaces` and `--asyncio-importers` run the full path once in a fresh interpreter and
print, respectively, the `os.replace` count by call site and every module that requests `asyncio`.
Either may run without `--baseline`.

Negative-spec:
    - Writes only under a directory it creates (under `--out-dir`, or the system temp dir); an
      out-dir inside the repo root is refused. The directory is removed at exit.
    - Never asserts or encodes a bar. The 300ms verdict is read from the printed report.
    - No `git worktree` and no `tar` binary: the baseline tree is read from git objects in process.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import shutil
import statistics
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.interleave import Primitive, run_interleaved
from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.benchmarks.tests.test_terminal_commit_process_budget import (
    K_INVOCATIONS,
    SAMPLES,
)
from coordinator_core.win_portability import no_console_creationflags

DRIVER = "coordinator_core.benchmarks.terminal_commit_full_path"
TREE_ROOT = "coordinator_core"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE_HEAD = """
import builtins, json, os, sys
from collections import Counter
from pathlib import Path
state = {"phase": "setup"}
asyncio_hits = Counter()
replace_sites = Counter()
real_import = builtins.__import__
real_replace = os.replace

def _hook(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0 and (name == "asyncio" or name.startswith("asyncio.")):
        who = (globals or {}).get("__name__", "?")
        if not who.startswith(("asyncio.", "importlib.")) and who != "asyncio":
            asyncio_hits[(who, state["phase"])] += 1
    return real_import(name, globals, locals, fromlist, level)

def _replace(src, dst, *a, **k):
    if state["phase"] == "call":
        f1, f2 = sys._getframe(1), sys._getframe(2)
        site = "%s:%s <- %s:%s" % (
            f1.f_globals.get("__name__", "?"), f1.f_code.co_name,
            f2.f_globals.get("__name__", "?"), f2.f_code.co_name,
        )
        replace_sites[site] += 1
    return real_replace(src, dst, *a, **k)

builtins.__import__ = _hook
os.replace = _replace
from coordinator_core.benchmarks import terminal_commit_full_path as drv
repo, script = drv.build_fixture(Path(sys.argv[1]))
drv._prepare_close(repo)
state["phase"] = "call"
out = drv._run_full_path(repo, script)
builtins.__import__ = real_import
os.replace = real_replace
ok = out.get("review_stamp") == "minted" and out.get("plan_status") == "implemented"
print("RESULT=" + json.dumps({
    "ok": ok,
    "asyncio_importers": [
        {"module": m, "phase": p, "requests": n} for (m, p), n in sorted(asyncio_hits.items())
    ],
    "replace_total": sum(replace_sites.values()),
    "replace_sites": dict(replace_sites.most_common()),
}))
"""


@dataclass(frozen=True)
class Side:
    name: str
    ref: str
    sha: Optional[str]
    measure: Callable[[], dict]


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return value


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="terminal_commit_ab", description="Interleaved A/B of the terminal-commit full path."
    )
    parser.add_argument("--baseline", help="sha (or ref) of side A; omit to run only the in-process probes")
    parser.add_argument("--rounds", type=positive_int, default=3)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--count-replaces", action="store_true")
    parser.add_argument("--asyncio-importers", action="store_true")
    args = parser.parse_args(list(argv))
    if not (args.baseline or args.count_replaces or args.asyncio_importers):
        parser.error("give --baseline, --count-replaces or --asyncio-importers")
    return args


def check_out_dir(out_dir: Path, repo_root: Path = _REPO_ROOT) -> Path:
    resolved = out_dir.resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError(f"--out-dir {resolved} is inside the repo root {root}; pass a scratch dir outside it")
    return resolved


def _rmtree(path: Path) -> None:
    def _chmod_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_chmod_retry)
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: _chmod_retry(f, p, e))


def resolve_sha(repo: Path, ref: str) -> str:
    """A full sha passes through. Any other ref costs one `git rev-parse` spawn (DR-344 per-use:
    ref resolution has no in-process reader for abbreviations, and it runs once, outside the
    measured window)."""
    if _FULL_SHA.match(ref):
        return ref
    done = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=str(repo), capture_output=True, text=True, check=False, **no_console_creationflags(),
    )
    sha = done.stdout.strip()
    if done.returncode != 0 or not _FULL_SHA.match(sha):
        raise ValueError(f"cannot resolve baseline {ref!r} to a commit: {done.stderr.strip()[:200]}")
    return sha


def _walk_tree(common_dir: Path, tree_sha: str, dest: Path, read_object, parse_entries) -> None:
    got = read_object(common_dir, tree_sha)
    entries = parse_entries(got[1]) if got and got[0] == "tree" else None
    if entries is None:
        raise LookupError(f"unreadable tree object {tree_sha}")
    dest.mkdir(parents=True, exist_ok=True)
    for name, (mode, sha) in entries.items():
        if mode == 0o040000:
            _walk_tree(common_dir, sha, dest / name, read_object, parse_entries)
        elif mode == 0o160000:
            continue
        else:
            blob = read_object(common_dir, sha)
            if blob is None or blob[0] != "blob":
                raise LookupError(f"unreadable blob {sha} for {name}")
            (dest / name).write_bytes(blob[1])


def _extract_via_git_objects(repo: Path, sha: str, dest: Path) -> None:
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.git_state import _parse_tree_entries

    common_dir = resolve_git_common_dir(repo)
    commit = read_object(common_dir, sha)
    if commit is None or commit[0] != "commit":
        raise LookupError(f"unreadable commit {sha}")
    first = commit[1].split(b"\n", 1)[0].decode("ascii", "replace")
    if not first.startswith("tree "):
        raise LookupError(f"commit {sha} has no tree line")
    root = read_object(common_dir, first[5:].strip())
    root_entries = _parse_tree_entries(root[1]) if root and root[0] == "tree" else None
    if root_entries is None or TREE_ROOT not in root_entries:
        raise LookupError(f"{TREE_ROOT}/ not found in {sha}")
    _walk_tree(common_dir, root_entries[TREE_ROOT][1], dest / TREE_ROOT, read_object, _parse_tree_entries)


def _extract_via_git_archive(repo: Path, sha: str, dest: Path) -> None:
    """Fallback when the object readers cannot walk the tree (e.g. a pack form they do not read).
    One `git archive` spawn (DR-344 per-use: no in-process equivalent exists for this case),
    outside the measured window; extracted with `tarfile`."""
    done = subprocess.run(
        ["git", "archive", "--format=tar", sha, TREE_ROOT],
        cwd=str(repo), capture_output=True, check=True, **no_console_creationflags(),
    )
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
        tar.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tar.extractall(dest)


def materialise_baseline(repo: Path, sha: str, dest: Path) -> Path:
    """Writes `coordinator_core/` as of `sha` under `dest`; returns `dest`."""
    try:
        _extract_via_git_objects(repo, sha, dest)
    except LookupError:
        if (dest / TREE_ROOT).exists():
            _rmtree(dest / TREE_ROOT)
        _extract_via_git_archive(repo, sha, dest)
    return dest


def _child_env(tree: Path) -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(tree) + (os.pathsep + existing if existing else "")
    return env


def assert_resolves_to_tree(tree: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", "import coordinator_core,sys;sys.stdout.write(coordinator_core.__file__)"],
        cwd=str(tree), env=_child_env(tree), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    got = Path(done.stdout.strip()).resolve()
    if tree.resolve() not in got.parents:
        raise RuntimeError(f"coordinator_core resolved to {got}, outside side tree {tree}")


def _sample(tree: Path, workdir: Path, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", DRIVER, str(workdir), *extra],
        k=K_INVOCATIONS, env=_child_env(tree), cwd=str(tree),
    )
    if result["rc"] != 0:
        raise RuntimeError(f"driver exited {result['rc']} in {tree}: {result!r}")
    return result


def measure_net(tree: Path, workdir: Path) -> dict:
    """One net reading: min of SAMPLES setup-only, min of SAMPLES full, the gate's own method."""
    def best(*extra: str) -> dict:
        return min((_sample(tree, workdir, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])

    setup = best("--setup-only")
    full = best()
    return {
        "net_ms": round(full["process_time_ms"] - setup["process_time_ms"], 3),
        "net_procs": round(full["procs_per_call"] - setup["procs_per_call"], 3),
    }


def run_ab(sides: Sequence[Side], rounds: int, rng: Optional[random.Random] = None) -> dict:
    """Interleaves `sides` for `rounds` rounds in a fresh shuffle per round; returns per-side
    nets, min, median, procs_per_call (of the min-net round) and the draw order."""
    order: list[str] = []
    readings: dict[str, list[dict]] = {s.name: [] for s in sides}

    def _draw(side: Side) -> Callable[[], float]:
        def invoke() -> float:
            order.append(side.name)
            reading = side.measure()
            readings[side.name].append(reading)
            return reading["net_ms"]
        return invoke

    run_interleaved([Primitive(s.name, _draw(s)) for s in sides], n=rounds, rng=rng)
    report: dict = {"rounds": rounds, "order": order, "sides": {}}
    for side in sides:
        got = readings[side.name]
        nets = [r["net_ms"] for r in got]
        best = min(got, key=lambda r: r["net_ms"])
        report["sides"][side.name] = {
            "ref": side.ref,
            "sha": side.sha,
            "nets_ms": nets,
            "min_ms": min(nets),
            "median_ms": statistics.median(nets),
            "procs_per_call": best["net_procs"],
        }
    return report


def run_probe(tree: Path, workdir: Path) -> dict:
    """The full path once in a fresh interpreter with `__import__` and `os.replace` wrapped."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE_HEAD, str(workdir)],
        cwd=str(tree), env=_child_env(tree), capture_output=True, text=True, timeout=300,
        **no_console_creationflags(),
    )
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT=")]
    if done.returncode != 0 or not lines:
        raise RuntimeError(f"probe failed rc={done.returncode}: {done.stderr[-800:]}")
    result = json.loads(lines[-1][len("RESULT="):])
    if not result["ok"]:
        raise RuntimeError("probe: the full path did not complete (review_stamp minted, plan_status implemented)")
    return result


def _host_context() -> dict:
    return {"platform": sys.platform, "cpu_count": os.cpu_count(), "k": K_INVOCATIONS, "samples": SAMPLES}


def main(argv: Sequence[str]) -> int:
    declare_benchmark_origin()
    args = parse_args(argv)
    out_parent = check_out_dir(args.out_dir) if args.out_dir is not None else None
    if out_parent is not None:
        out_parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="terminal-commit-ab-", dir=str(out_parent) if out_parent else None))
    try:
        from coordinator_core.git.git_state import head_sha

        head = head_sha(_REPO_ROOT)
        report: dict = {"head_sha": head, "host": _host_context()}
        if args.count_replaces or args.asyncio_importers:
            probe_dir = work / "probe"
            probe_dir.mkdir()
            probe = run_probe(_REPO_ROOT, probe_dir)
            if args.count_replaces:
                report["replaces"] = {"total": probe["replace_total"], "sites": probe["replace_sites"]}
            if args.asyncio_importers:
                report["asyncio_importers"] = probe["asyncio_importers"]
        if args.baseline:
            sha = resolve_sha(_REPO_ROOT, args.baseline)
            base_tree = materialise_baseline(_REPO_ROOT, sha, work / "baseline")
            assert_resolves_to_tree(base_tree)
            assert_resolves_to_tree(_REPO_ROOT)
            (work / "wd-a").mkdir()
            (work / "wd-b").mkdir()
            sides = [
                Side("A", args.baseline, sha, lambda: measure_net(base_tree, work / "wd-a")),
                Side("B", "working-tree", head, lambda: measure_net(_REPO_ROOT, work / "wd-b")),
            ]
            report["ab"] = run_ab(sides, args.rounds)
        print(json.dumps(report, indent=2))
        return 0
    finally:
        _rmtree(work)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
