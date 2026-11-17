"""Profile the gate-tree builder at HEAD: .git root present, toplevel resolution, process time, git spawns.

Builds the tree through `baseline_export.clone_tree`. Also reads HEAD's commit and tree through
`coordinator_core.git.git_objects` in a scratch `--shared --no-checkout` clone to prove alternates
are followed. Prints one JSON line. Process time is os.times() self+children CPU, never wall clock.
Runs as `python -m coordinator_core.benchmarks.gate_tree_clone_profile`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from coordinator_core.git import git_objects  # noqa: E402
from coordinator_core.git.run import run_git  # noqa: E402
from coordinator_core.testing import baseline_export  # noqa: E402

_GIT_ENV_KEYS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES")


def _cpu_ms() -> float:
    t = os.times()
    return (t.user + t.system + t.children_user + t.children_system) * 1000.0


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_ENV_KEYS}


def _builder() -> tuple[str, Callable[[str, str, Path], None]]:
    """(name, callable) for the gate-tree builder."""
    return "clone_tree", baseline_export.clone_tree


def measure_build(toplevel: str, sha40: str, root: Path) -> dict:
    """Build the gate tree at `root`; process ms and git spawns of the build only."""
    name, build = _builder()
    spawns = 0
    real_popen = baseline_export._git_popen

    def counting(args: list[str], cwd: str | os.PathLike[str], **kw) -> object:
        nonlocal spawns
        spawns += 1
        return real_popen(args, cwd, **kw)

    setattr(baseline_export, "_git_popen", counting)
    before = _cpu_ms()
    try:
        build(toplevel, sha40, root)
    finally:
        setattr(baseline_export, "_git_popen", real_popen)
    process_ms = round(_cpu_ms() - before, 1)
    return {"builder": name, "process_ms": process_ms, "git_spawns": spawns}


def inspect_tree(root: Path) -> dict:
    """has_dot_git, and what `git rev-parse --show-toplevel` says from inside the tree with no ceiling set."""
    done = run_git(["rev-parse", "--show-toplevel"], cwd=os.fspath(root), env=_clean_env())
    top = done.stdout.strip() if done.ok else None
    return {
        "has_dot_git": (root / ".git").exists(),
        "show_toplevel_rc": done.returncode,
        "show_toplevel": top,
        "show_toplevel_is_root": bool(top) and os.path.realpath(top) == os.path.realpath(root),
    }


def alternates_read(toplevel: str, sha40: str, scratch: Path) -> dict:
    """Clone --shared --no-checkout and read sha40's commit and root tree through git_objects.

    ok only when the clone's own store misses the commit (so the read had to follow alternates) and
    the alternates-following read returns the commit and its tree.
    """
    clone = scratch / "alt-clone"
    done = run_git(["clone", "-q", "--shared", "--no-checkout", toplevel, os.fspath(clone)])
    if not done.ok:
        return {"alternates_read_ok": False, "alternates_why": f"clone failed: {done.stderr.strip()}"}
    common = clone / ".git"
    local_miss = git_objects._read_local_object(common, sha40) is None
    commit = git_objects.read_object(common, sha40)
    if commit is None or commit[0] != "commit":
        return {"alternates_read_ok": False, "local_miss": local_miss, "alternates_why": "commit unreadable"}
    first = commit[1].split(b"\n", 1)[0].decode("ascii", "replace")
    tree_sha = first.split(" ", 1)[1] if first.startswith("tree ") else ""
    tree = git_objects.read_object(common, tree_sha) if tree_sha else None
    ok = local_miss and tree is not None and tree[0] == "tree" and len(tree[1]) > 0
    return {"alternates_read_ok": ok, "local_miss": local_miss, "tree_sha": tree_sha}


def main(argv: list[str] | None = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sha", default="HEAD", help="commit to build the gate tree at (default HEAD)")
    args = ap.parse_args(argv)
    parsed = run_git(["rev-parse", "--verify", "--end-of-options", f"{args.sha}^{{commit}}"])
    if not parsed.ok:
        print(f"gate_tree_clone_profile: cannot resolve {args.sha!r}: {parsed.stderr.strip()}", file=sys.stderr)
        return 1
    toplevel, sha40 = baseline_export.resolve(parsed.stdout.strip(), os.getcwd())
    scratch = baseline_export.export_root(toplevel, sha40, os.getpid()).with_name(f"gate-tree-profile-{os.getpid()}")
    root = scratch / "tree"
    row: dict = {"sha": sha40}
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        row.update(measure_build(toplevel, sha40, root))
        row.update(inspect_tree(root))
        row.update(alternates_read(toplevel, sha40, scratch))
    finally:
        baseline_export.remove(scratch)
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
