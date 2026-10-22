"""Budget pin for plan.merge: a fresh-process first call merging a 14 + 9 row pair and writing.

The child imports `coordinator_core.ipc` (the engine is up), then inside one `time.process_time()`
bracket imports the op module and makes one `_handler` call that merges and writes. Bars: zero
spawns and process time under the manifest's `plan.merge` target x (1 + tolerance).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO / "coordinator_core" / "benchmarks" / "budget-manifest.json"
_A_ROWS = 14
_B_ROWS = 9

_CHILD = r"""
import json, sys, time
from pathlib import Path
import coordinator_core.ipc  # noqa: F401
import pytest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

root = Path(sys.argv[1])
params = json.loads(sys.argv[2])
with pytest.MonkeyPatch.context() as mp, _count_spawns_attributed(mp) as spawns:
    t0 = time.process_time()
    from coordinator_core.ops import plan_merge as op
    reply = op._handler(params, root)
    elapsed = time.process_time() - t0
print(json.dumps({"process_s": elapsed, "spawns": len(spawns), "ok": reply.get("ok"),
                  "out_rows": (reply.get("report") or {}).get("counts", {}).get("out")}))
"""


def _row(rid: str) -> str:
    return (
        f'- id: {rid}\n  title: "writer {rid}"\n  change_kind: code-edit\n  surface: src/{rid}.py\n'
        f"  writes:\n    - src/{rid}.py\n  disposition: open\n"
        f"  body: |\n    write the file for {rid}, with the prose a real row carries.\n"
        f"    A second line of body text so the row is not a one-liner.\n"
    )


def _plan(prefix: str, count: int, title: str) -> str:
    rows = "".join(_row(f"{prefix}{i}") for i in range(count))
    prose = "".join(f"Preamble paragraph {i} of {title}: context a reader skims.\n\n" for i in range(12))
    return (
        f'---\ntitle: "{title}"\ncreated: "2026-10-22"\nauthor: EM\nstatus: draft\n---\n\n'
        f"# {title}\n\n{prose}## Tasks\n\n```yaml plan-tasks\n{rows}```\n"
    )


def _write_fixture(root: Path) -> dict:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "a.md").write_text(_plan("A", _A_ROWS, "A"), encoding="utf-8")
    (plans / "b.md").write_text(_plan("B", _B_ROWS, "B"), encoding="utf-8")
    delta = {
        "sources": [
            {"path": "docs/plans/a.md", "origin_label": "a"},
            {"path": "docs/plans/b.md", "origin_label": "b"},
        ],
        "frontmatter": {"title": "M", "created": "2026-10-22", "author": "EM", "status": "draft"},
        "dropped_sections": [
            {"heading": "(preamble)", "source": "docs/plans/a.md", "reason": "superseded"},
            {"heading": "(preamble)", "source": "docs/plans/b.md", "reason": "superseded"},
        ],
    }
    (plans / "d.json").write_text(json.dumps(delta), encoding="utf-8")
    (root / ".git").mkdir(exist_ok=True)
    return {"sources": ["docs/plans/a.md", "docs/plans/b.md"], "delta": "docs/plans/d.json",
            "out": "docs/plans/out.md"}


def _bar_s() -> float:
    entry = json.loads(_MANIFEST.read_text(encoding="utf-8"))["overrides"]["plan.merge"]
    assert entry["tolerance"]["kind"] == "relative"
    return entry["target_ms"] / 1000.0 * (1 + entry["tolerance"]["value"])


def test_first_merge_of_a_14_plus_9_row_pair_meets_the_budget(tmp_path):
    params = _write_fixture(tmp_path)
    sources_before = {s: (tmp_path / s).read_bytes() for s in params["sources"]}

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), json.dumps(params)],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got["ok"] is True and got["out_rows"] == _A_ROWS + _B_ROWS, got
    assert (tmp_path / params["out"]).is_file()
    assert got["spawns"] == 0, got
    assert got["process_s"] < _bar_s(), got
    assert {s: (tmp_path / s).read_bytes() for s in params["sources"]} == sources_before
