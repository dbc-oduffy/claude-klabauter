"""sizing-assemble process-time benchmark: the scout's `sizing-assemble --write <routed sizing> --json` call.

Builds a tmp git repo holding one routed sizing (`status: routed`, `plan:` edge set, one register row)
and its plan, then drives the bin trampoline's `main` in-process the way the ask scout does (appetite,
tshirt, intent, premise flags, `--write`, `--json`). Reports process ms (`time.process_time`, never wall
clock), the child-spawn count, and how many warm-door hops (`warm.client.try_warm_dispatch`, the only function wrapped)
the call made: a `-32004` can only come from such a hop.
`measure()` is the single entry the pin test drives. Figures are in-process CPU: a spawned child's CPU is
not in them, only its spawn count. The engine-root resolver is pinned to this checkout because a fixture
repo has no published engine to resolve.

Run: `python -m coordinator_core.benchmarks.sizing_assemble_process_budget [--runs N] [--enforce-bar]`.
Scratch lives in a managed temp dir this script removes.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import io
import statistics
import sys
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.git.run import run_git

#: The brightline process-time bar (CLAUDE.md § The brightline).
BAR_MS = 500.0
DEFAULT_RUNS = 5
SIZING_REL = "state/sizings/2026-11/2026-11-06-sizing-assemble-bench.yaml"
PLAN_REL = "docs/plans/2026-11/2026-11-06-sizing-assemble-bench.md"
CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[2]
_BIN = CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin"
#: Door hops: (module, attribute) wrapped to count calls. A `-32004` is minted only inside the first.
DOOR_HOPS = (
    ("coordinator_core.warm.client", "try_warm_dispatch"),
)
_ASSEMBLE_ARGV = (
    "--appetite", "medium", "--tshirt", "M", "--intent", "bench: assemble a routed sizing",
    "--premise-provenance", "read", "--premise-evidence", "fixture",
)


def _git(root: Path, *args: str) -> None:
    res = run_git(["-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.example", *args])
    if not res.ok:
        raise RuntimeError(f"git {' '.join(args[:2])} failed (rc={res.returncode}): {res.stderr.strip()}")


def build_fixture(root: Path) -> None:
    """Populate `root` (an empty dir): a git repo with one committed routed sizing and its linked plan."""
    sizing = {
        "schema": "sizing-object", "name": "sizing assemble bench", "intent": "exercise sizing-assemble on a routed sizing",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan", "detents": [],
        "fork": None, "xl_exit": None, "status": "routed", "plan": PLAN_REL,
        "premise": {"provenance": "read", "evidence": "fixture"},
        "deliverable_id": "dlv-sizing-assemble-bench-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
        "requirement_register": {
            "sources": [{"doc": SIZING_REL, "ref": "ask"}],
            "rows": [{"id": "R1", "source_text": "bench the assemble", "anchor": "relayed",
                      "surface": "internal", "status": "open", "claimed_by": []}],
        },
    }
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    for rel, text in (
        (SIZING_REL, yaml.safe_dump(sizing, sort_keys=False)),
        (PLAN_REL, "---\ntitle: sizing assemble bench\n---\n\n# bench\n"),
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed (sizing-assemble bench)")


def _load_trampoline():
    """The bin trampoline module, its `entry_point_shim` engine-root resolver pinned to this checkout."""
    for path in (str(_BIN), str(_BIN / "lib")):
        if path not in sys.path:
            sys.path.insert(0, path)
    shim = importlib.import_module("entry_point_shim")
    spec = importlib.util.spec_from_file_location("sizing_assemble_bench_trampoline", _BIN / "sizing-assemble.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return shim, mod


@contextlib.contextmanager
def _counted_hops(mp: pytest.MonkeyPatch, counts: dict):
    """Wrap each door-hop function so every call adds one to `counts[attribute]`."""
    for modname, attr in DOOR_HOPS:
        owner = importlib.import_module(modname)
        real = getattr(owner, attr, None)
        if real is None:
            continue

        def counted(*a, _real=real, _attr=attr, **k):
            counts[_attr] = counts.get(_attr, 0) + 1
            return _real(*a, **k)

        mp.setattr(owner, attr, counted)
    yield


def _call_once(repo: Path) -> dict:
    """One scout-shaped `sizing-assemble` call on `repo`; returns process ms, spawns, door hops, exit code."""
    shim, tramp = _load_trampoline()
    hops: dict = {}
    mp = pytest.MonkeyPatch()
    out = io.StringIO()
    try:
        mp.chdir(repo)
        mp.setattr(shim, "_cc_invoke_resolve_claude_klabauter_root", lambda: (lambda: str(CLAUDE_KLABAUTER_ROOT)))
        spawn_mp = pytest.MonkeyPatch()
        with _counted_hops(mp, hops), _count_spawns_attributed(spawn_mp) as spawns, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            t0 = time.process_time()
            rc = tramp.main([*_ASSEMBLE_ARGV, "--write", SIZING_REL, "--json"])
            total = (time.process_time() - t0) * 1000.0
    finally:
        mp.undo()
    return {
        "process_ms": total, "spawns": len(spawns), "spawn_argv": [s.argv for s in spawns],
        "door_hops": sum(hops.values()), "exit_code": rc, "stdout": out.getvalue(),
    }


def _reset(repo: Path) -> None:
    """Drop what the previous call wrote so each measured call starts from the committed fixture."""
    _git(repo, "checkout", "-q", "HEAD", "--", SIZING_REL)
    _git(repo, "clean", "-fdxq", "--", "scratch", ".coordinator-local")


def measure(runs: int = DEFAULT_RUNS) -> dict:
    """Build the fixture, then a cold call and the median of `runs` warm calls; returns the report."""
    from coordinator_core.temp_layout import make_managed_temp_dir

    scratch = make_managed_temp_dir(prefix="sizing-assemble-bench-")
    try:
        root = scratch / "repo"
        root.mkdir()
        build_fixture(root)
        with fixture_settings_home(scratch), pytest.MonkeyPatch.context() as mp:
            for key in ("COORDINATOR_SESSION_ID", "DELIVERABLE_ID"):
                mp.delenv(key, raising=False)
            _reset(root)
            cold = _call_once(root)
            samples = []
            for _ in range(runs):
                _reset(root)
                samples.append(_call_once(root))
        mid = sorted(samples, key=lambda s: s["process_ms"])[len(samples) // 2]
        return {
            "cold_process_ms": round(cold["process_ms"], 1),
            "warm_process_ms": round(statistics.median(s["process_ms"] for s in samples), 1),
            "process_ms": round(mid["process_ms"], 1),
            "cold_spawns": cold["spawns"],
            "spawns": mid["spawns"],
            "spawn_argv": mid["spawn_argv"],
            "door_hops": max([cold["door_hops"], *(s["door_hops"] for s in samples)]),
            "exit_code": mid["exit_code"],
            "cold_exit_code": cold["exit_code"],
            "stdout": mid["stdout"],
        }
    finally:
        rmtree_or_raise(scratch, label="sizing-assemble-bench")



def main() -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    ap.add_argument("--enforce-bar", action="store_true", help=f"exit 2 when cold or warm process ms >= {BAR_MS:.0f}")
    args = ap.parse_args()
    res = measure(args.runs)
    for key, value in res.items():
        if key not in ("spawn_argv", "stdout"):
            print(f"{key}: {value}")
    for argv in res["spawn_argv"]:
        print(f"spawn: {' '.join(argv)[:160]}")
    over = max(res["warm_process_ms"], res["cold_process_ms"]) >= BAR_MS
    print(f"bar: {'OVER' if over else 'UNDER'} {BAR_MS:.0f}ms process time (cold {res['cold_process_ms']}, warm {res['warm_process_ms']})")
    return 2 if args.enforce_bar and over else 0


if __name__ == "__main__":
    sys.exit(main())
