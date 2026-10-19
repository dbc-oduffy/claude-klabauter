"""Budget pin for plan.overlap_edges: a fresh-process first call over 200 certified plans.

The child imports `coordinator_core.ipc` (the engine is up), then inside one `time.process_time()`
bracket imports the op module and makes one `_overlap_edges` call over plan texts it has not read.
Bars: under 0.2s process time and 0 spawns (APM ruling 2026-10-19: 200ms blocks build).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO = Path(__file__).resolve().parents[3]
_PLAN_COUNT = 200
_MIN_EDGES = 158
_BAR_S = 0.2

_CHILD = r"""
import json, sys, time
from pathlib import Path
import coordinator_core.ipc  # noqa: F401
import pytest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

root = Path(sys.argv[1])
rels = json.loads(sys.argv[2])
with pytest.MonkeyPatch.context() as mp, _count_spawns_attributed(mp) as spawns:
    t0 = time.process_time()
    from coordinator_core.ops import plan_overlap_edges as op
    reply = op._overlap_edges({"plans": rels}, root)
    elapsed = time.process_time() - t0
print(json.dumps({"process_s": elapsed, "spawns": len(spawns),
                  "edges": len(reply["edges"]), "set": len(reply["set"]),
                  "unreadable": len(reply["unreadable"])}))
"""


def _write_fixture(root: Path) -> list[str]:
    """200 certified plans: a shared hub chain plus a pair-shared path and a private path each."""
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    rels = []
    for i in range(_PLAN_COUNT):
        name = f"2026-10-19-plan-{i:03d}"
        rows = (
            f"- id: C{i}a\n  title: hub writer\n  surface: src/hub.py\n  change_kind: code-edit\n"
            f"  writes:\n    - src/hub.py\n    - src/own_{i}.py\n  disposition: open\n"
            f"  body: |\n    write the hub\n"
            f"- id: C{i}b\n  title: pair writer\n  surface: src/pair_{i // 2}.py\n"
            f"  change_kind: code-edit\n  writes: [src/pair_{i // 2}.py]\n  disposition: open\n"
            f"  depends_on:\n    - chunk: C{i}a\n      gate_kind: output-consumption-runtime\n"
            f"  body: |\n    write the pair file\n"
        )
        text = (
            f"---\ntitle: plan {i}\nstatus: approved\nmise_prepped_sha: {i:040x}\n---\n\n"
            f"## Tasks\n\n```yaml plan-tasks\n{rows}```\n"
        )
        (plans / f"{name}.md").write_text(text, encoding="utf-8")
        rels.append(f"docs/plans/{name}.md")
    return rels


def test_first_call_over_200_certified_plans_meets_the_budget(tmp_path):
    rels = _write_fixture(tmp_path)
    before = {r: (tmp_path / r).read_bytes() for r in rels}

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), json.dumps(rels)],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["set"] == _PLAN_COUNT and got["unreadable"] == 0
    assert got["edges"] >= _MIN_EDGES, got
    assert got["spawns"] == 0, got
    assert got["process_s"] < _BAR_S, got
    assert {r: (tmp_path / r).read_bytes() for r in rels} == before
