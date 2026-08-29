"""coordinator_core.benchmarks.git_config_probe -- what each git performance
setting is actually worth on a 35k-entry index, measured WARM in a throwaway
clone.

WHY A CLONE, NOT THIS REPO. Two reasons, and only the first is about safety.
`core.untrackedCache` and `index.version` mutate the index format in place; a
peer reading the shared index mid-rewrite is a hazard nobody needs to take for a
measurement. And `core.fsmonitor` -- which this module can measure but does NOT
enable by default -- starts a persistent daemon, which CLAUDE.md's Load norm
reserves as a call to surface rather than take. A clone that is reaped on
teardown answers the question and leaves nothing running.

WHY WARM, AND WHAT THE ONE-SHOT `-c` TRAP COSTS. `core.untrackedCache` lives IN
the index. A one-shot `git -c core.untrackedCache=true status` never populates
it, so it measures the cost of enabling the cache and none of its benefit --
which is how a prior measurement pass concluded, wrongly, that it does nothing.
Every arm here writes its settings into the clone's own config and runs the
command several times before sampling, so the cache under test is populated by
the time it is timed.

NEVER WALL CLOCK. Child-process CPU only, via `process_time.batched_process_time_
quantiles`, per DR-344. This box carries ~50 concurrent sessions; wall clock
measures that load, not git. The baton this descends from timed `git --version`
at 81.7ms wall against `git add` at 28.0ms wall in the same run.

NEGATIVE SPEC -- this module does not:
  - enable anything in the repo it is invoked from; every write lands in the clone
  - leave a clone, or any process rooted in one, alive past its own teardown
  - measure `core.fsmonitor` unless explicitly asked (`--include-fsmonitor`)
  - report a single sample; every arm is quantiles over n batches
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from coordinator_core.benchmarks import isolated_clone
from coordinator_core.benchmarks.process_time import batched_process_time_quantiles

#: The command under test. `--porcelain` is the shape every scripted caller uses
#: and the one the 265.6ms baseline was taken against; a bare `git status` adds
#: branch-ahead/behind formatting that is not the scan being measured.
PROBE_CMD: Sequence[str] = ("git", "status", "--porcelain")

#: Arms, in the order reported. `None` value means "unset this key", so an arm
#: never inherits a prior arm's setting -- each arm is applied against a clean
#: config, but the key list is unioned across arms so an unset is explicit.
ARMS: Dict[str, Dict[str, Optional[str]]] = {
    "baseline": {},
    "untrackedCache": {"core.untrackedCache": "true"},
    "preloadIndex": {"core.preloadIndex": "true"},
    "indexVersion4": {"index.version": "4"},
    "manyFiles": {"feature.manyFiles": "true"},
    "no_daemon_stanza": {
        "core.untrackedCache": "true",
        "core.preloadIndex": "true",
        "index.version": "4",
    },
}

#: Only measured when explicitly requested. Starts `git fsmonitor--daemon` in the
#: clone; teardown reaps it. Never merged into ARMS by default -- see the module
#: docstring's negative spec.
FSMONITOR_ARM: Dict[str, Optional[str]] = {"core.fsmonitor": "true"}

#: Every key any arm touches. Reset to unset before each arm so arms cannot leak
#: into one another through the clone's persistent CONFIG. Index FORMAT is a
#: separate axis -- see `index_version` -- and is not reset by this list; an
#: arm that wrote index.version=4 leaves a v4 index on disk for the next arm
#: to warm against until git next rewrites it. Each arm's report attests the
#: index version it actually ran with (`index_version` field) rather than
#: assuming isolation on that axis.
ALL_KEYS = sorted(
    {key for arm in ARMS.values() for key in arm} | set(FSMONITOR_ARM)
)


def index_version(clone: Path) -> Optional[int]:
    """Read the on-disk index format version out of `.git/index`'s header.

    `_apply_arm` can only unset CONFIG. Index FORMAT is not config -- it lives in
    the file, and git rewrites it on its next write, not when the key is unset.
    So an arm that wrote v4 leaves a v4 index behind for the next arm to warm
    against. Reading the version back is the only way an arm's report can be
    trusted rather than assumed; without it, `effective_config` attests the half
    that was never in doubt.

    Header is `DIRC` + a 4-byte big-endian version. Returns None if unreadable.
    """
    try:
        with open(clone / ".git" / "index", "rb") as handle:
            header = handle.read(8)
    except OSError:
        return None
    if len(header) < 8 or header[:4] != b"DIRC":
        return None
    return int.from_bytes(header[4:8], "big")


def _git(clone: Path, *args: str, check: bool = True) -> str:
    """Run a git command in `clone` and return stdout."""
    proc = subprocess.run(
        ("git", *args),
        cwd=str(clone),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            "git %s failed in %s (rc=%d): %s"
            % (" ".join(args), clone, proc.returncode, proc.stderr.strip())
        )
    return proc.stdout


def _apply_arm(clone: Path, settings: Dict[str, Optional[str]]) -> None:
    """Reset every key this module knows about, then set this arm's."""
    for key in ALL_KEYS:
        _git(clone, "config", "--unset-all", key, check=False)
    for key, value in settings.items():
        if value is not None:
            _git(clone, "config", key, value)


def _warm(clone: Path, rounds: int) -> None:
    """Run the probe command enough times that any index-resident cache the arm
    enabled is populated before a single sample is taken.

    This is the whole reason the module exists rather than a `-c` one-liner.
    """
    for _ in range(rounds):
        _git(clone, *PROBE_CMD[1:], check=False)


def measure_arm(
    clone: Path, name: str, settings: Dict[str, Optional[str]], *, k: int, n: int, warm: int
) -> dict:
    """Apply one arm, warm it, and return its quantiles plus the config that was
    actually in effect (read back from git, never assumed from the input dict)."""
    _apply_arm(clone, settings)
    _warm(clone, warm)
    result = batched_process_time_quantiles(PROBE_CMD, k=k, n=n, cwd=str(clone))
    effective = {}
    for key in ALL_KEYS:
        value = _git(clone, "config", "--get", key, check=False).strip()
        effective[key] = value or None
    return {
        "arm": name,
        "p50_ms": round(result["p50_ms"], 1),
        "p90_ms": round(result["p90_ms"], 1),
        "samples": [round(s, 1) for s in result["samples"]],
        "k": result["k"],
        "n": result["n"],
        "effective_config": effective,
        # Review: coordinator:code-reviewer -- config isolation between arms is
        # verified above via `effective_config`, but index FORMAT is not config
        # and unsetting a key does not retroactively rewrite the index. This
        # attests the index version actually in effect for this arm's warm-up
        # and sample, rather than assuming it matches the arm's own setting.
        "index_version": index_version(clone),
    }


def run(
    source_root: Path, *, k: int, n: int, warm: int, include_fsmonitor: bool
) -> dict:
    """Clone `source_root`, measure every arm warm, and reap the clone.

    The clone and any process rooted under it are torn down in `finally`, so an
    arm that raises mid-measurement still leaves nothing behind -- which is the
    property that makes measuring `core.fsmonitor` here safe at all.
    """
    clone = isolated_clone.mkdtemp_for_clone(source_root, prefix="git-config-probe")
    arms = dict(ARMS)
    if include_fsmonitor:
        arms["fsmonitor"] = FSMONITOR_ARM
    try:
        subprocess.run(
            ("git", "clone", "--no-hardlinks", "--local", str(source_root), str(clone)),
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        entries = len(_git(clone, "ls-files").splitlines())
        results: List[dict] = []
        for name, settings in arms.items():
            results.append(
                measure_arm(clone, name, settings, k=k, n=n, warm=warm)
            )
        return {
            "source_root": str(source_root),
            "index_entries": entries,
            "cmd": list(PROBE_CMD),
            "k": k,
            "n": n,
            "warm_rounds": warm,
            "arms": results,
        }
    finally:
        reaped = isolated_clone.reap_processes_under(clone)
        isolated_clone.rmtree_or_raise(clone, label="git-config-probe", reaped=reaped)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repo to clone and measure")
    parser.add_argument(
        "--k", type=int, default=5, help="invocations per batch (amortises the 15.625ms Windows quantum)"
    )
    parser.add_argument("--n", type=int, default=5, help="batches per arm, for quantiles")
    parser.add_argument(
        "--warm", type=int, default=3, help="untimed runs per arm, to populate index-resident caches"
    )
    parser.add_argument(
        "--include-fsmonitor",
        action="store_true",
        help="also measure core.fsmonitor -- starts a daemon INSIDE the clone; reaped on teardown",
    )
    args = parser.parse_args(argv)

    report = run(
        Path(args.repo).resolve(),
        k=args.k,
        n=args.n,
        warm=args.warm,
        include_fsmonitor=args.include_fsmonitor,
    )
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
