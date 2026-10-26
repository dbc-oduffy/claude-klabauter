"""Drive one terminal commit's whole hold computation on a real repo root and count its git spawns.

    python -m coordinator_core.benchmarks.measure -- python coordinator_core/benchmarks/measure_terminal_hold.py <root>

The composition is the sibling-claim pass (`sibling_claims.sibling_run_claims`) plus the halted-run
leg `terminal_commit._live_run_claims` makes (`halted_run_write_claims`), under one
`release_scope` when `halted_hold_release` has one. Candidates are the paths dirty in <root>,
read before the measured region. Prints `git_spawns=<n> claims=<n>` for the region only.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402


def _dirty_paths(root: Path) -> list:
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "-z", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    paths = []
    for entry in out.split("\0"):
        if len(entry) > 3:
            paths.append(entry[3:])
    return paths


def main(argv: list) -> int:
    declare_benchmark_origin()
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    candidates = _dirty_paths(root)

    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import halted_hold_release, sibling_claims

    spawns = []
    real_init = subprocess.Popen.__init__

    def counting_init(self, args, *a, **kw):
        first = args[0] if isinstance(args, (list, tuple)) and args else args
        if Path(str(first)).stem == "git":
            spawns.append(1)
        real_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = counting_init
    try:
        with getattr(halted_hold_release, "release_scope", contextlib.nullcontext)():
            claims = sibling_claims.sibling_run_claims(root, "measure-terminal-hold", candidates)
            halted = warp_run_deliverable.halted_run_write_claims(
                root, candidates, exclude_run="measure-terminal-hold",
            )
    finally:
        subprocess.Popen.__init__ = real_init
    print(f"git_spawns={len(spawns)} claims={len(claims)} halted={len(halted or {})} candidates={len(candidates)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
