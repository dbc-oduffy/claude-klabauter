"""Process-time and spawn profile of the `repo.setup` op on a fresh and an already-onboarded root.

Runs `coordinator_core.ops.repo_setup.handle` in-process over a throwaway `git init` tree with
HOME, USERPROFILE and CLAUDE_CONFIG_DIR pinned under the same temp dir. Process time is this
process's CPU plus descendants (`commit_leg_profile._ChildMeter`); wall clock is never recorded.
Every `subprocess.Popen` construction is counted and named by the op step that was running.

One JSON row per axis: `{axis, process_time_ms, spawns, attribution}`; axes `fresh` and `rerun`
are whole-op rows, `attribution.steps` carries per-step `{process_time_ms, spawns}` and
`attribution.spawn_argv` the argv[0] basename plus first argument of each spawn by step.
`BAR_MS` is the DR-344 brightline; a whole-op row over it exits 1.

    python coordinator_core/benchmarks/repo_setup_budget_profile.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

BAR_MS = 500.0
AXES = ("fresh", "rerun")


@dataclass
class Row:
    axis: str
    process_time_ms: float
    spawns: int
    attribution: dict = field(default_factory=dict)


class _SpawnLog:
    """Counts `subprocess.Popen` constructions, tagged with the step that is running."""

    def __init__(self) -> None:
        self.step = "(outside-steps)"
        self.argv: Dict[str, List[str]] = {}
        self._orig = None

    def install(self) -> None:
        self._orig = subprocess.Popen.__init__
        log = self
        orig = self._orig

        def counted(popen_self, args, *a, **kw):
            first = args if isinstance(args, (str, bytes, os.PathLike)) else (list(args) or [""])
            if isinstance(first, list):
                label = " ".join([os.path.basename(str(first[0]))] + [str(x) for x in first[1:2]])
            else:
                label = os.path.basename(str(first))
            log.argv.setdefault(log.step, []).append(label)
            return orig(popen_self, args, *a, **kw)

        subprocess.Popen.__init__ = counted

    def restore(self) -> None:
        if self._orig is not None:
            subprocess.Popen.__init__ = self._orig
            self._orig = None


def _pin_home(base: Path) -> Dict[str, str | None]:
    home = base / "home"
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    pinned = {"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(claude_home)}
    saved = {k: os.environ.get(k) for k in pinned}
    os.environ.update(pinned)
    return saved


def _unpin_home(saved: Dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _measure(axis: str, root: Path, mod, log: _SpawnLog) -> Row:
    from coordinator_core.benchmarks.commit_leg_profile import _ChildMeter

    steps: Dict[str, dict] = {}
    original = mod._STEPS

    def wrap(step):
        name = step.__name__.removeprefix("_step_")

        def timed(ctx):
            before_spawns = sum(len(v) for v in log.argv.values())
            started = time.process_time()
            log.step = name
            try:
                return step(ctx)
            finally:
                steps[name] = {
                    "process_time_ms": round((time.process_time() - started) * 1000.0, 3),
                    "spawns": sum(len(v) for v in log.argv.values()) - before_spawns,
                }
                log.step = "(outside-steps)"

        timed.__name__ = step.__name__
        return timed

    mod._STEPS = tuple(wrap(s) for s in original)
    log.argv.clear()
    meter = _ChildMeter()
    meter.start()
    own0 = time.process_time()
    try:
        verdict = mod.handle({"root": str(root)})
    finally:
        own_ms = (time.process_time() - own0) * 1000.0
        child_ms = meter.stop_ms()
        mod._STEPS = original
    return Row(
        axis=axis,
        process_time_ms=round(own_ms + child_ms, 3),
        spawns=sum(len(v) for v in log.argv.values()),
        attribution={
            "steps": steps,
            "spawn_argv": {k: list(v) for k, v in log.argv.items()},
            "created": len(verdict["created"]),
            "open": len(verdict["open"]),
            "needs_attention": len(verdict["needs_attention"]),
            "child_process_time_ms": child_ms,
        },
    )


def profile(base: Path) -> List[Row]:
    """Measure `repo.setup` on a fresh git root, then again on the same (now onboarded) root."""
    from coordinator_core.ops import repo_setup as mod

    root = base / "target"
    root.mkdir(parents=True, exist_ok=True)
    from coordinator_core.git.run import run_git

    init = run_git(["init", "-q", str(root)])
    if init.returncode != 0:
        raise RuntimeError("git init failed: %s" % init.stderr.strip())
    saved = _pin_home(base)
    log = _SpawnLog()
    log.install()
    try:
        return [_measure(axis, root, mod, log) for axis in AXES]
    finally:
        log.restore()
        _unpin_home(saved)


def over_bar(rows: List[Row]) -> bool:
    """True when any whole-op row is over `BAR_MS` process time (DR-344 kill gate)."""
    return any(r.process_time_ms > BAR_MS for r in rows)


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dir", help="work dir (default: a temp dir removed on exit)")
    args = ap.parse_args(argv)
    if args.dir:
        base = Path(args.dir)
        base.mkdir(parents=True, exist_ok=True)
        rows = profile(base)
    else:
        with tempfile.TemporaryDirectory(prefix="repo-setup-profile-") as tmp:
            rows = profile(Path(tmp))
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2, sort_keys=True))
    else:
        for r in rows:
            print(f"{r.axis:6s} process={r.process_time_ms:9.1f}ms spawns={r.spawns:3d}")
            for step, cost in r.attribution["steps"].items():
                print(f"    {step:14s} {cost['process_time_ms']:8.1f}ms spawns={cost['spawns']}")
    if over_bar(rows):
        print(f"KILL-GATE: repo.setup over {BAR_MS:.0f}ms process time (DR-344)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
