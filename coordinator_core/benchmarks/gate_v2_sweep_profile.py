"""roadmap.plan_gate sweep (shape B) falsifier: process time, spawns, cProfile, N/2N scaling, report digests.

Measures `roadmap.plan_gate._handler({}, root)` in process time (`process_time.in_process_time_ms`,
never wall clock): one warm-up, then `RUNS` warm samples, a spawn count with `subprocess.Popen`
patched, and a cold figure from one call in a fresh interpreter. Two corpora: the live tree, and a
synthetic live-shaped one generated from a fixed template at N and 2N handoffs, so scaling and the
behaviour digests do not drift with the live corpus.

    python coordinator_core/benchmarks/gate_v2_sweep_profile.py [--n 500] [--json]

Prints shape B median/max process_time_ms, spawn count, the cold figure, cProfile top-20 by
cumtime, t(N), t(2N) and the per-added-baton ms, and sha256 digests of the shape A-D reports. The synthetic-corpus digests
are the behaviour pin a cost cut must leave unchanged; the live digests are comparable only on an
unchanged live corpus.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
import pstats
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS = 7
N_DEFAULT = 500
SCALING_K = 64
BASE_SHA = "24ee6f09ae"

_GROUP = 10


def _handoff(i: int, group: int, slot: int) -> str:
    """One live-shaped handoff head. Blockers stay inside the group so closure depth is bounded."""
    base = f"synth-{group:04d}"
    name = f"{base}-{slot}"
    lines = [
        "---",
        f'title: "{name}"',
        "created: 2026-11-01",
        'branch: "work/synth/2026-11-01"',
        "status: open",
        "predecessor: none",
        "kind: roadmap-seed",
        "category: infra",
        f'goals: "goal-synth-{group % 7}"',
        'summary: "Synthetic roadmap seed used only to size the plan-gate sweep."',
        f'roadmap_id: "rm-synth-{group % 13}"',
        f'stub_id: "{name}"',
        f'handoff_id: "hnd-{name}"',
        f'deliverable_id: "dlv-{name}"',
    ]
    state, role = "awaiting_gate", "work"
    blocked: List[str] = []
    extra: List[str] = []
    if slot == 0:
        state = "shipped"
    elif slot in (1, 2):
        blocked = [f"{base}-0"]
        extra.append(f'sizing_object: "state/sizings/{name}.yaml"')
    elif slot in (3, 4):
        state = "ready_to_fire"
        blocked = [f"{base}-1"]
    elif slot == 5:
        state = "in_flight"
    elif slot == 6:
        role = "record"
    elif slot == 7:
        extra.append("superseded_by: none-yet-but-set-synth")
    elif slot == 8:
        blocked = [f"{base}-3", f"{base}-4"]
        extra.append(f'governing_plan: "docs/plans/2026-11/{name}.md"')
    lines.append(f"baton_role: {role}")
    lines.append(f"deployment_state: {state}")
    if blocked:
        lines.append("blocked_by:")
        lines.extend(f"  - {b}" for b in blocked)
    lines.extend(extra)
    lines.extend(
        [
            "gate_evidence:",
            "  covers_prose: true",
            "  legs:",
            "    - leg_id: synth-leg",
            "      kind: human",
            "      reason: >-",
            "        Synthetic leg text that pads the header to a live-like length so the",
            "        head reader sees a realistic block.",
            "---",
            "",
            f"# {name}",
            "",
        ]
    )
    return "\n".join(lines)


def _plan(name: str, approved: bool) -> str:
    return "\n".join(
        [
            "---",
            f'title: "plan {name}"',
            f"status: {'approved' if approved else 'draft'}",
            f'plan_id: "plan-{name}"',
            f'deliverable_id: "dlv-{name}"',
            "---",
            "",
            f"# plan {name}",
            "",
        ]
    )


def _sizing(name: str) -> str:
    return f"id: {name}\nroute: plan  # dispatch | spec-dispatch | plan\nsize: M\n"


def build_corpus(root: Path, n: int) -> Path:
    """Write `n` live-shaped handoffs (plus their plans and sizings) under `root`; return `root`."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    handoffs = root / "state" / "handoffs"
    plans = root / "docs" / "plans" / "2026-11"
    sizings = root / "state" / "sizings"
    for d in (handoffs, plans, sizings, root / "archive" / "handoffs", root / "archive" / "sizings"):
        d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        group, slot = divmod(i, _GROUP)
        name = f"synth-{group:04d}-{slot}"
        (handoffs / f"2026-11-01-{name}.md").write_text(_handoff(i, group, slot), encoding="utf-8")
        if slot in (1, 2):
            (sizings / f"{name}.yaml").write_text(_sizing(name), encoding="utf-8")
        if slot in (2, 3, 8):
            (plans / f"{name}.md").write_text(_plan(name, approved=slot != 3), encoding="utf-8")
    return root


def shape_params(root: Path) -> Dict[str, dict]:
    """Shapes A (roadmap_id), B (sweep), C (subject), D (roadmap_id, the SEAM 1 cut)."""
    from coordinator_core.ops import roadmap_plan_gate as op

    batons = op._handler({}, root)["batons"]
    if not batons:
        raise RuntimeError(f"no baton under {root}; a cost figure over nothing proves nothing")
    rid = _first_roadmap_id(root)
    return {"A": {"roadmap_id": rid}, "B": {}, "C": {"subject": batons[0]["id"]}, "D": {"roadmap_id": rid}}


def _first_roadmap_id(root: Path) -> str:
    pattern = re.compile(r"^roadmap_id:[ \t]*['\"]?([^\s'\"#]+)", re.MULTILINE)
    base = root / "state" / "handoffs"
    for path in sorted(base.glob("*.md")) + sorted(base.glob("*/*.md")):
        found = pattern.search(path.read_text(encoding="utf-8", errors="replace")[:2048])
        if found:
            return found.group(1)
    raise RuntimeError(f"no handoff under {base} carries a roadmap_id")


def report_digests(root: Path) -> Dict[str, str]:
    from coordinator_core.ops import roadmap_plan_gate as op

    out = {}
    for shape, params in shape_params(root).items():
        blob = json.dumps(op._handler(dict(params), root), sort_keys=True, default=str)
        out[shape] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return out


def sweep_samples(root: Path, runs: int = RUNS, k: Optional[int] = None) -> List[float]:
    """`runs` warm shape-B process-time samples (ms) after one untimed warm-up call."""
    from coordinator_core.benchmarks.process_time import in_process_time_ms
    from coordinator_core.ops import roadmap_plan_gate as op

    op._handler({}, root)
    return [in_process_time_ms(lambda: op._handler({}, root), k=k, warmup=0)["process_time_ms"] for _ in range(runs)]


def sweep_median_ms(root: Path, runs: int = RUNS) -> float:
    return statistics.median(sweep_samples(root, runs))


def spawn_count(root: Path) -> int:
    """`subprocess.Popen` creations during one shape-B call."""
    from coordinator_core.ops import roadmap_plan_gate as op

    spawns: List[tuple] = []
    real = subprocess.Popen

    def _count(*args, **kwargs):
        spawns.append(args)
        return real(*args, **kwargs)

    subprocess.Popen = _count  # type: ignore[assignment]
    try:
        op._handler({}, root)
    finally:
        subprocess.Popen = real  # type: ignore[assignment]
    return len(spawns)


def cprofile_top(root: Path, top: int = 20) -> str:
    from coordinator_core.ops import roadmap_plan_gate as op

    op._handler({}, root)
    prof = cProfile.Profile()
    prof.enable()
    op._handler({}, root)
    prof.disable()
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("cumulative").print_stats(top)
    return buf.getvalue()


def cold_ms(root: Path) -> Dict[str, float]:
    """One shape-B call in a fresh interpreter: `import_ms` (imports) and `call_ms` (the first call)."""
    code = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "t0=time.process_time()\n"
        "from coordinator_core.ops import roadmap_plan_gate as op\n"
        "t1=time.process_time()\n"
        "op._handler({}, Path(sys.argv[1]))\n"
        "t2=time.process_time()\n"
        "print(json.dumps({'import_ms':(t1-t0)*1000,'call_ms':(t2-t1)*1000,'total_ms':t2*1000}))\n"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
        creationflags=flags,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def scaling(n: int = N_DEFAULT, runs: int = 5) -> Dict[str, float]:
    """Best-of-`runs` shape-B process time over synthetic corpora of `n` and `2n` handoffs."""
    with tempfile.TemporaryDirectory(prefix="gate-v2-sweep-") as tmp:
        small = build_corpus(Path(tmp) / "n", n)
        large = build_corpus(Path(tmp) / "2n", 2 * n)
        t_n = min(sweep_samples(small, runs, SCALING_K))
        t_2n = min(sweep_samples(large, runs, SCALING_K))
    return {
        "n": n,
        "t_n_ms": t_n,
        "t_2n_ms": t_2n,
        "ratio": t_2n / t_n if t_n else float("inf"),
        "per_added_baton_ms": (t_2n - t_n) / n,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=N_DEFAULT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT))
    live = REPO_ROOT
    samples = sweep_samples(live)
    result = {
        "base_sha": BASE_SHA,
        "shape_b_live": {
            "median_ms": statistics.median(samples),
            "max_ms": max(samples),
            "samples_ms": samples,
            "spawns": spawn_count(live),
            "cold": cold_ms(live),
        },
        "scaling": scaling(args.n),
        "digests_live": report_digests(live),
    }
    with tempfile.TemporaryDirectory(prefix="gate-v2-sweep-") as tmp:
        result["digests_synthetic"] = report_digests(build_corpus(Path(tmp), args.n))
    top = cprofile_top(live)
    if args.json:
        result["cprofile_top20"] = top
        print(json.dumps(result, indent=2))
        return 0
    b = result["shape_b_live"]
    print(f"base {BASE_SHA}; shape B live: median {b['median_ms']:.1f}ms max {b['max_ms']:.1f}ms "
          f"spawns {b['spawns']} cold import {b['cold']['import_ms']:.1f}ms call {b['cold']['call_ms']:.1f}ms")
    s = result["scaling"]
    print(f"scaling: t({s['n']})={s['t_n_ms']:.1f}ms t({2 * s['n']})={s['t_2n_ms']:.1f}ms ratio={s['ratio']:.2f} "
          f"per-added-baton={s['per_added_baton_ms']:.3f}ms")
    print("digests live:", json.dumps(result["digests_live"]))
    print("digests synthetic:", json.dumps(result["digests_synthetic"]))
    print("cProfile top-20 by cumtime (live, shape B):")
    print(top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
