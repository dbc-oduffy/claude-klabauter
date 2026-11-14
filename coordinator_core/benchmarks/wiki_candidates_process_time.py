"""Process-time falsifier for the wiki-candidate ops (`ceremony.wiki_update_candidates`, `ceremony.wiki_candidates`).

Runs the target op's default `{}` invocation warm in-process against a repo: one discarded warm-up
call, then N measured runs (default 3). Per run: self CPU (`time.process_time`), child CPU
(`RUSAGE_CHILDREN` delta, so the git processes the op spawns are counted), their sum `total_ms`, and
the spawns by git subcommand through the shared `spawn_counter` Popen patch. Never wall clock. Where
`resource` is unavailable (Windows) child_ms and total_ms are null, never 0, and the bench exits 2.

The target module is `coordinator_core/ops/ceremony/<op suffix>.py` and its handler
`_ceremony_<op suffix>`. With `--sha` the module is read from `git show <sha>:<path>` into an
in-memory module (nothing lands in the tree or `sys.modules`, no op re-registration); that measures
the old op after it is deleted from the tree. Without `--sha` the live module is imported.

Exit 0 when max total_ms < BAR_MS and no history-walk spawn (`log`, `rev-list`, `bisect`) occurred;
1 otherwise; 2 when unmeasured.

Run: `python -m coordinator_core.benchmarks.wiki_candidates_process_time
[--op NAME] [--sha SHA] [--runs N] [--repo PATH]`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import types
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

BAR_MS = 500.0
DEFAULT_RUNS = 3
DEFAULT_OP = "ceremony.wiki_candidates"
OLD_OP = "ceremony.wiki_update_candidates"
OLD_OP_SHA = "af18e4fbc38ec06a83155110999f4356d4e1efe9"
OP_PACKAGE = "coordinator_core.ops.ceremony"
HISTORY_WALK_KINDS = frozenset({"log", "rev-list", "bisect"})
_GIT_OPTS_WITH_VALUE = frozenset({"-c", "-C", "--git-dir", "--work-tree"})


def op_suffix(op: str) -> str:
    """`wiki_candidates` from `ceremony.wiki_candidates`; ValueError for any other op family."""
    family, _, suffix = op.partition(".")
    if family != "ceremony" or not suffix:
        raise ValueError(f"op must be ceremony.<name>; got {op!r}")
    return suffix


def load_op_module(repo: Path, op: str, sha: Optional[str] = None) -> types.ModuleType:
    """The op's module: imported live, or as it stood at `sha` executed into a fresh module object.

    The `sha` form does not touch `sys.modules`; `ipc.register_op` is a pass-through while the source
    runs, so the live registry entry survives. Raises RuntimeError when git cannot show the blob.
    """
    suffix = op_suffix(op)
    if sha is None:
        return importlib.import_module(f"{OP_PACKAGE}.{suffix}")
    from coordinator_core import ipc
    from coordinator_core.git.run import run_git

    module_path = f"coordinator_core/ops/ceremony/{suffix}.py"
    res = run_git(["show", f"{sha}:{module_path}"], cwd=str(repo), timeout=30)
    if not res.ok:
        raise RuntimeError(f"git show {sha}:{module_path} failed: {res.stderr.strip()[:200]}")
    module = types.ModuleType(f"{OP_PACKAGE}.{suffix}")
    module.__package__ = OP_PACKAGE
    module.__file__ = f"<baseline {sha}:{module_path}>"
    real = ipc.register_op

    def passthrough(name, handler=None, params_schema=None):
        return handler if handler is not None else (lambda fn: fn)

    ipc.register_op = passthrough
    try:
        exec(compile(res.stdout, module.__file__, "exec"), module.__dict__)  # noqa: S102 - our own committed blob
    finally:
        ipc.register_op = real
    return module


def spawn_kind(argv: tuple[str, ...]) -> str:
    """The git subcommand of a spawned argv (`log`, `grep`, `cat-file`), else the program basename."""
    if not argv:
        return "?"
    if Path(argv[0]).stem != "git":
        return Path(argv[0]).stem
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in _GIT_OPTS_WITH_VALUE:
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg
    return "git"


def _child_cpu_ms() -> Optional[float]:
    if resource is None:
        return None
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (ru.ru_utime + ru.ru_stime) * 1000.0


def _count_spawns(call: Callable[[], Any]) -> tuple[Any, dict[str, int]]:
    import pytest

    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

    mp = pytest.MonkeyPatch()
    with _count_spawns_attributed(mp) as spawns:
        result = call()
    return result, dict(Counter(spawn_kind(s.argv) for s in spawns))


def one_run(call: Callable[[], dict]) -> tuple[dict[str, Any], dict]:
    """One measured call: (row{self_ms, child_ms, total_ms, spawns, kinds}, reply). child/total null off-POSIX."""
    child0 = _child_cpu_ms()
    start = time.process_time()
    reply, kinds = _count_spawns(call)
    self_ms = (time.process_time() - start) * 1000.0
    child1 = _child_cpu_ms()
    child_ms = None if child0 is None or child1 is None else round(child1 - child0, 2)
    total_ms = None if child_ms is None else round(self_ms + child_ms, 2)
    row = {"self_ms": round(self_ms, 2), "child_ms": child_ms, "total_ms": total_ms,
           "spawns": sum(kinds.values()), "kinds": kinds}
    return row, reply


def measure(call: Callable[[], dict], runs: int = DEFAULT_RUNS) -> tuple[list[dict[str, Any]], dict]:
    """A discarded warm-up, then `runs` measured calls; (rows, last reply)."""
    call()
    rows: list[dict[str, Any]] = []
    reply: dict = {}
    for _ in range(runs):
        row, reply = one_run(call)
        rows.append(row)
    return rows, reply


def max_total_ms(rows: list[dict[str, Any]]) -> Optional[float]:
    """Largest per-run total_ms; None when any run is unmeasured."""
    totals = [r["total_ms"] for r in rows]
    return None if not totals or any(t is None for t in totals) else max(totals)


def run(repo: Path, op: str = DEFAULT_OP, sha: Optional[str] = None, runs: int = DEFAULT_RUNS) -> dict[str, Any]:
    """Measure `op`'s default `{}` invocation over `repo`; the JSON report."""
    module = load_op_module(repo, op, sha)
    handler = getattr(module, f"_ceremony_{op_suffix(op)}")
    rows, reply = measure(lambda: handler({}, repo_root=repo), runs)
    kinds: Counter = Counter()
    for r in rows:
        for kind, n in r["kinds"].items():
            kinds[kind] = max(kinds[kind], n)
    walks = sorted(k for k in kinds if k in HISTORY_WALK_KINDS)
    peak = max_total_ms(rows)
    return {
        "bar_ms": BAR_MS,
        "op": op,
        "sha": sha,
        "runs": rows,
        "max_total_ms": peak,
        "spawns": max(r["spawns"] for r in rows),
        "kinds": dict(kinds),
        "history_walk_kinds": walks,
        "window": reply.get("window"),
        "pages": len(reply.get("pages", [])),
        "pass": peak is not None and peak < BAR_MS and not walks,
        "unmeasured": peak is None,
    }


def exit_code(report: dict[str, Any]) -> int:
    """2 when unmeasured, 0 when the report passes, else 1."""
    if report["unmeasured"]:
        return 2
    return 0 if report["pass"] else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--op", default=DEFAULT_OP)
    parser.add_argument("--sha", default=None, help=f"measure the op as of this commit (old op: {OLD_OP_SHA})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = run(args.repo.resolve(), args.op, args.sha, args.runs)
    print(json.dumps(report, indent=2))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
