"""Cost profile of `run_scratch_reap.reap_run` over a 200-path ledger with 50 keep entries.

The fixture is a tmp repo (fake v2 git index) holding a landed run whose ledger lists 200 plain
scratch dirs plus 50 keeps (25 unexpired, 25 expired at the reap moment), and `scratch/warp/<run>`.
Two rows, each over a fresh fixture, are measured over the call alone (imports and fixture
construction are outside the span):

  base  the reaper absent (baseline_ref `BASELINE_REF` has no `run_scratch_reap` module): nothing
        is called, 0 paths removed
  end   `reap_run` at the working tree

Row fields: `{label, removed, kept, process_time_ms, spawns, listings}`. `process_time_ms` is
`time.process_time` of this process (the reap spawns no child); `spawns` counts `Popen` inits;
`listings` counts `os.scandir`/`os.listdir`/`os.walk` calls. Wall clock is never recorded.

    python coordinator_core/benchmarks/run_scratch_reap_profile.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BAR_MS = 500.0
BASELINE_REF = "c9e2ef11c8"
REAPER_MODULE_REL = "coordinator_core/ops/dispatch_emit/run_scratch_reap.py"
RUN_ID = "ask-20261109T000000-aaaaaa"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
LEDGER_PATHS = 200
KEEPS_LIVE = 25
KEEPS_EXPIRED = 25


@dataclass
class ReapRow:
    """One measured reap."""

    label: str
    removed: int
    kept: int
    process_time_ms: float
    spawns: int
    listings: int


@dataclass
class Fixture:
    """A built tmp repo: its root and the scratch paths on disk before the reap."""

    root: Path
    paths: list


def _touch_dir(root: Path, rel: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "f.txt").write_text("x", encoding="utf-8")


def build_fixture(tmp_path: Path) -> Fixture:
    """A landed run with `LEDGER_PATHS` ledger dirs, `KEEPS_LIVE + KEEPS_EXPIRED` keeps and its warp dir."""
    from coordinator_core.ops.dispatch_emit import run_scratch

    root = Path(tmp_path)
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "index").write_bytes(b"DIRC" + (2).to_bytes(4, "big"))
    record = root / "state" / "warp" / RUN_ID
    record.mkdir(parents=True, exist_ok=True)
    (record / "halted.json").write_text(json.dumps({"status": "landed"}), encoding="utf-8")

    paths = [f"scratch/warp/{RUN_ID}"]
    plain = [f"scratch/land-{i:04d}" for i in range(LEDGER_PATHS)]
    run_scratch.record_paths(root, RUN_ID, plain)
    paths += plain
    for i in range(KEEPS_LIVE + KEEPS_EXPIRED):
        rel = f"scratch/probe-keep-{i:03d}"
        live = i < KEEPS_LIVE
        kept_at = NOW - timedelta(hours=1 if live else 5)
        run_scratch.declare_keep(root, RUN_ID, rel, "profile keep", 48 if live else 1, now=kept_at)
        paths.append(rel)
    for rel in paths:
        _touch_dir(root, rel)
    return Fixture(root, paths)


class _Meter:
    """Counts `Popen` inits and directory listings while active; restores every patched seam on exit."""

    def __init__(self) -> None:
        self.spawns = 0
        self.listings = 0

    def __enter__(self) -> "_Meter":
        self._popen_init = subprocess.Popen.__init__
        self._listers = {name: getattr(os, name) for name in ("scandir", "listdir", "walk")}
        meter = self
        popen_init = self._popen_init

        def counted_init(proc, *a, **k):
            meter.spawns += 1
            return popen_init(proc, *a, **k)

        def counting(fn):
            def wrapper(*a, **k):
                meter.listings += 1
                return fn(*a, **k)

            return wrapper

        subprocess.Popen.__init__ = counted_init
        for name, fn in self._listers.items():
            setattr(os, name, counting(fn))
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._popen_init
        for name, fn in self._listers.items():
            setattr(os, name, fn)


def measure(label: str, fixture: Fixture, reap: Optional[Callable[[Path, str, datetime], dict]]) -> ReapRow:
    """Run `reap(root, run_id, now)` once over `fixture` (None: the reaper is absent, nothing runs).
    `removed` is read from the disk afterwards, not from the reaper's own report."""
    with _Meter() as meter:
        cpu0 = time.process_time()
        out = reap(fixture.root, RUN_ID, NOW) if reap is not None else {}
        cpu = time.process_time() - cpu0
    gone = sum(1 for rel in fixture.paths if not (fixture.root / rel).exists())
    kept = len(out.get("kept", [])) if isinstance(out, dict) else 0
    return ReapRow(label, gone, kept, round(cpu * 1000.0, 3), meter.spawns, meter.listings)


def baseline_has_reaper(ref: str = BASELINE_REF) -> Optional[bool]:
    """Whether `ref` carries the reaper module; None when git cannot answer. Spawns git: outside the measured span."""
    from coordinator_core.git.run import run_git

    probe = run_git(["cat-file", "-e", f"{ref}:{REAPER_MODULE_REL}"], cwd=str(_REPO))
    if probe.ok:
        return True
    return False if "exists on disk, but not in" in (probe.stderr or "") or "does not exist" in (probe.stderr or "") else None


def run(workdir: Path) -> list:
    """The base and end rows, each over its own fresh fixture under `workdir`."""
    from coordinator_core.ops.dispatch_emit import run_scratch_reap

    base_fx = build_fixture(workdir / "base")
    end_fx = build_fixture(workdir / "end")
    return [measure("base", base_fx, None), measure("end", end_fx, run_scratch_reap.reap_run)]


def over_bar(rows: list) -> bool:
    """True when the end row breaches the bar: process time at or over `BAR_MS`, any spawn, any listing."""
    return any(r.label == "end" and (r.process_time_ms >= BAR_MS or r.spawns or r.listings) for r in rows)


def render(rows: list) -> str:
    out = [f"{'row':6} {'removed':>8} {'kept':>6} {'proc_ms':>9} {'spawns':>6} {'listings':>8}"]
    for r in rows:
        out.append(f"{r.label:6} {r.removed:8d} {r.kept:6d} {r.process_time_ms:9.1f} {r.spawns:6d} {r.listings:8d}")
    return "\n".join(out)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dir", default=None)
    args = ap.parse_args(argv)

    from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

    declare_benchmark_origin()
    workdir = Path(tempfile.mkdtemp(prefix="rsrp-", dir=args.dir))
    try:
        with fixture_settings_home(workdir):
            rows = run(workdir / "fx")
    finally:
        rmtree_or_raise(workdir, label="run-scratch-reap-profile")
    print(json.dumps([asdict(r) for r in rows], indent=1) if args.json else render(rows))
    print(f"baseline {BASELINE_REF} carries the reaper: {baseline_has_reaper()}")
    if over_bar(rows):
        print(f"OVER-BAR: end row breaches {BAR_MS:.0f}ms / 0 spawns / 0 listings (DR-344)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
