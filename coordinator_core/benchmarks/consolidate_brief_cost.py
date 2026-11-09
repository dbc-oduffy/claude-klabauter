"""Cost instrument for `consolidate_assemble.brief`: process-tree CPU, git spawns, gather shapes.

    python -m coordinator_core.benchmarks.consolidate_brief_cost <repo> [--no-shapes] [--shape-timeout S]
    python -m coordinator_core.benchmarks.consolidate_brief_cost --calibrate <scratch-dir>

Prints exactly one JSON line on stdout and writes no file:
`{process_ms, git_spawns, sha_count, ref_count, shapes}`. `process_ms` is user+kernel CPU of
this process tree across the `brief()` call (Windows job object via `LiveTreeAccountant`; POSIX
`getrusage` self+children), never wall clock. `git_spawns` counts the injected `run_git` calls.
`ref_count` is the branch refs `brief()` enumerates; `sha_count` is the deduped sha set it hands
`inspect_commits`.
`shapes` times each candidate one-spawn gather over that sha set, fed on stdin;
a shape that outlives `--shape-timeout` is killed and reported `timed_out` (the instrument's own
guard, not a product ceiling).

Trap: job accounting is tick-quantised (~15.6ms); a figure near a tick multiple is the instrument.
`--calibrate` builds a tiny fixture repo under the given directory, removes it, and exits non-zero
unless the spawn count, sha count and a synthetic known-count workload all reconcile.
"""

from __future__ import annotations

import json
import os
import stat
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.win_portability import no_console_creationflags

#: label -> argv (after `git`); the sha set is fed on stdin, one per line.
SHAPES: dict[str, list[str]] = {
    "show_stat": ["show", "--stat", "--stdin"],
    "log_stat": ["log", "--no-walk=unsorted", "--stdin", "--stat"],
    "log_shortstat": ["log", "--no-walk=unsorted", "--stdin", "--shortstat"],
    "log_header": ["log", "--no-walk=unsorted", "--stdin"],
}

DEFAULT_SHAPE_TIMEOUT_S = 120.0


class _TreeClock:
    """Cumulative process-tree CPU in ms: `read()` deltas bracket a window."""

    def __init__(self) -> None:
        self._accountant = None
        if sys.platform == "win32":
            from coordinator_core.benchmarks.process_time import LiveTreeAccountant

            self._accountant = LiveTreeAccountant(os.getpid())

    def read(self) -> float:
        if self._accountant is not None:
            return float(self._accountant.snapshot()["process_time_ms"])
        import resource

        own = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime) * 1000.0

    def close(self) -> None:
        if self._accountant is not None:
            self._accountant.close()


class CountingRunGit:
    """`run_git` wrapper: delegates to `inner` and records every call's argv."""

    def __init__(self, inner: Callable[..., Any]) -> None:
        self.inner = inner
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, input=None):  # noqa: A002 - mirrors RunGit
        self.calls.append(list(args))
        if input is None:
            return self.inner(args, cwd)
        return self.inner(args, cwd, input=input)

    @property
    def spawns(self) -> int:
        return len(self.calls)


def _measure_brief(repo: Path, clock: _TreeClock) -> dict[str, Any]:
    from coordinator_core import consolidate_assemble as ca

    counting = CountingRunGit(ca.default_run_git)
    captured: list[str] = []
    original = ca.inspect_commits

    def recording(run_git, repo_root, shas):
        captured.extend(shas)
        return original(run_git, repo_root, shas)

    ca.inspect_commits = recording
    try:
        before = clock.read()
        ca.brief(repo_root=repo, run_git=counting)
        process_ms = clock.read() - before
    finally:
        ca.inspect_commits = original
    return {
        "process_ms": round(process_ms, 1),
        "git_spawns": counting.spawns,
        "shas": list(dict.fromkeys(captured)),
    }


def _ref_count(repo: Path) -> int:
    from coordinator_core import consolidate_assemble as ca

    return len(ca.list_branches(ca.default_run_git, repo))


def _measure_shapes(repo: Path, shas: list[str], clock: _TreeClock, timeout_s: float) -> dict[str, Any]:
    stdin = "".join(f"{sha}\n" for sha in shas)
    out: dict[str, Any] = {}
    for label, argv in SHAPES.items():
        timed_out = False
        before = clock.read()
        proc = subprocess.Popen(
            ["git", *argv], cwd=str(repo), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **no_console_creationflags(),
        )
        try:
            stdout, _ = proc.communicate(stdin.encode(), timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            stdout, _ = proc.communicate()
        process_ms = clock.read() - before
        out[label] = {
            "process_ms": round(process_ms, 1),
            "spawns": 1,
            "bytes": len(stdout),
            "rc": proc.returncode,
            "timed_out": timed_out,
        }
    return out


def measure(repo: Path, *, shapes: bool = True, shape_timeout_s: float = DEFAULT_SHAPE_TIMEOUT_S) -> dict[str, Any]:
    """One measurement record for `repo`; see the module docstring for the keys."""
    declare_benchmark_origin()
    repo = Path(repo).resolve()
    ref_count = _ref_count(repo)
    clock = _TreeClock()
    try:
        brief_cost = _measure_brief(repo, clock)
        shas = brief_cost.pop("shas")
        shape_costs = _measure_shapes(repo, shas, clock, shape_timeout_s) if shapes and shas else {}
    finally:
        clock.close()
    return {**brief_cost, "sha_count": len(shas), "ref_count": ref_count, "shapes": shape_costs}


_FIXTURE_BRANCHES = 3
_FIXTURE_COMMITS_PER_BRANCH = 4


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _fast_import_commit(ref: str, message: str, path: str, content: str, parent: Optional[str], stamp: int) -> str:
    """One fast-import `commit` command adding `path`; `parent` is a ref/mark or None (chain/root)."""
    msg = message.encode("utf-8")
    body = content.encode("utf-8")
    lines = [
        f"commit {ref}",
        f"committer t <t@example.com> {stamp} +0000",
        f"data {len(msg)}",
        message,
    ]
    if parent is not None:
        lines.append(f"from {parent}")
    lines += [f"M 100644 inline {path}", f"data {len(body)}", content]
    return "\n".join(lines) + "\n"


def _build_fixture(repo: Path) -> int:
    """main plus stale branches with unique commits, built in a constant number of git spawns
    (init, one fast-import stream on stdin, one checkout); returns the expected sha_count."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    stamp = 1_700_000_000
    stream = [_fast_import_commit("refs/heads/main", "base", "base.txt", "base\n", None, stamp)]
    for b in range(_FIXTURE_BRANCHES):
        for c in range(_FIXTURE_COMMITS_PER_BRANCH):
            stamp += 1
            stream.append(
                _fast_import_commit(
                    f"refs/heads/stale-{b}", f"b{b} c{c}", f"b{b}_{c}.txt", f"{b}-{c}\n",
                    "refs/heads/main" if c == 0 else None, stamp,
                )
            )
    subprocess.run(
        ["git", "fast-import", "--quiet"], cwd=str(repo), input="".join(stream).encode("utf-8"),
        capture_output=True, check=True, **no_console_creationflags(),
    )
    _git(repo, "checkout", "-q", "-f", "main")
    return _FIXTURE_BRANCHES * _FIXTURE_COMMITS_PER_BRANCH


def _rmtree(path: Path) -> None:
    """Remove a fixture repo; git object files are read-only on Windows, so chmod and retry."""
    def _onexc(func, p, _exc):
        try:
            if os.name == "nt":  # POSIX: a mode change here can strip a directory's traverse bit
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_onexc)
    else:
        shutil.rmtree(path, onerror=lambda func, p, _info: _onexc(func, p, None))


def calibrate(parent: Path) -> dict[str, Any]:
    """Instrument self-check on a fixture repo; raises SystemExit on any mismatch."""
    from coordinator_core import consolidate_assemble as ca

    repo = Path(parent).resolve() / "consolidate-brief-cost-fixture"
    if repo.exists():
        _rmtree(repo)
    try:
        expected_shas = _build_fixture(repo)
        # Known-count workload: the counting wrapper must read exactly what was issued.
        probe = CountingRunGit(ca.default_run_git)
        for _ in range(5):
            probe(["--version"], repo)
        if probe.spawns != 5:
            raise SystemExit(f"calibration: counting wrapper read {probe.spawns} for 5 calls")
        result = measure(repo, shapes=True)
        # brief on this fixture: independent recount from the spawn log of a second run.
        recount = CountingRunGit(ca.default_run_git)
        ca.brief(repo_root=repo, run_git=recount)
        if result["git_spawns"] != recount.spawns:
            raise SystemExit(f"calibration: spawns {result['git_spawns']} != recount {recount.spawns}")
        if result["sha_count"] != expected_shas:
            raise SystemExit(f"calibration: sha_count {result['sha_count']} != fixture {expected_shas}")
        if result["ref_count"] != _FIXTURE_BRANCHES + 1:
            raise SystemExit(f"calibration: ref_count {result['ref_count']} != {_FIXTURE_BRANCHES + 1}")
        for label, shape in result["shapes"].items():
            if shape["rc"] != 0 or shape["timed_out"] or shape["bytes"] == 0:
                raise SystemExit(f"calibration: shape {label} did not succeed: {shape}")
        return {**result, "calibrated": True}
    finally:
        _rmtree(repo)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    shapes = "--no-shapes" not in args
    args = [a for a in args if a != "--no-shapes"]
    timeout_s = DEFAULT_SHAPE_TIMEOUT_S
    if "--shape-timeout" in args:
        i = args.index("--shape-timeout")
        timeout_s = float(args[i + 1])
        del args[i:i + 2]
    if "--calibrate" in args:
        args.remove("--calibrate")
        if len(args) != 1:
            print("usage: --calibrate <scratch-dir>", file=sys.stderr)
            return 2
        declare_benchmark_origin()
        print(json.dumps(calibrate(Path(args[0]))))
        return 0
    if len(args) != 1:
        print("usage: consolidate_brief_cost <repo> [--no-shapes] [--shape-timeout S]", file=sys.stderr)
        return 2
    print(json.dumps(measure(Path(args[0]), shapes=shapes, shape_timeout_s=timeout_s)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
