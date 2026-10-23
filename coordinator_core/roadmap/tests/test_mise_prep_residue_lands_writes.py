"""The residue workflow lands exactly the paths its stages wrote, and the rulings op declares its plans."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.roadmap.tests.test_mise_prep_residue_workflow import SOURCE, _args, _bind
from coordinator_core.session.declared_writes import collecting

_PLAN = "---\nstatus: open\ntitle: t\n---\n\nbody\n"


def test_rulings_handler_declares_exactly_the_applied_plans(tmp_path, monkeypatch):
    from coordinator_core.ops import plan_record_residue_rulings as op

    (tmp_path / "docs" / "plans").mkdir(parents=True)
    ok = tmp_path / "docs" / "plans" / "ok.md"
    ok.write_text(_PLAN, encoding="utf-8")
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(op, "main_worktree_root", lambda r: str(tmp_path))
    rulings = [
        {"id": "census[0]", "item_kind": "census-leftover", "plan": "docs/plans/ok.md", "ruling": "drop", "pmOnly": False},
        {"id": "census[1]", "item_kind": "census-leftover", "plan": "docs/plans/missing.md", "ruling": "drop", "pmOnly": False},
        {"id": "census[2]", "item_kind": "census-leftover", "plan": "../escape.md", "ruling": "drop", "pmOnly": False},
    ]
    with collecting() as got:
        reply = op._handler({"run_id": "r", "on": "2026-10-23", "rulings": rulings}, repo_root=tmp_path)
    assert [a["plan"] for a in reply["applied"]] == ["docs/plans/ok.md"]
    assert len(reply["refused"]) == 2
    assert [Path(p).resolve() for p in got] == [ok.resolve()]


def _run_land(tmp_path: Path, args: dict, plans_reply: list, applied: list):
    bound = _bind(tmp_path, args)
    body = bound.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    harness = tmp_path / "run.cjs"
    harness.write_text(
        "const calls = [];\n"
        "const agent = async (p, o) => { calls.push([o.label, p]);\n"
        "  if (o.label === 'apm-rulings') return { rulings: [{ item: 'census[1]', ruling: 'drop', plan: 'docs/plans/p0.md', pmOnly: false }] };\n"
        "  if (o.label === 'persist-rulings') return { applied: " + json.dumps(applied) + ", refused: [] };\n"
        "  if (o.label === 'land-residue') return { committed: true, sha: 'abc' };\n"
        "  return { plans: " + json.dumps(plans_reply) + " }; };\n"
        "const phase = async () => {};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;\n"
        f"const body = {json.dumps(body)};\n"
        "new AsyncFunction('agent', 'phase', body)(agent, phase).then((r) => {\n"
        "  console.log(JSON.stringify({ calls, r }));\n"
        "}, (e) => { console.error('REJECTED: ' + e.message); process.exit(3); });\n",
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _plan_row(plan, declared=(), rewrote=()):
    return {"plan": plan, "declared": list(declared), "rewrote": list(rewrote), "leftoverCensus": [], "escalate": []}


def test_source_lands_through_commit_v2_only():
    text = SOURCE.read_text(encoding="utf-8")
    assert "ceremony.commit_v2" in text
    assert '["commit"' not in text and "'commit'," not in text
    assert "landPaths.length > 0" in text


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_land_stage_takes_only_the_union_deduplicated(tmp_path):
    args = _args(batches=2)
    args["batches"][0]["plans"][0]["seamRef"] = "state/mise-workflows/r.seam/p0.txt"
    plans = [_plan_row("docs/plans/p0.md", declared=["x"]), _plan_row("docs/plans/untouched.md")]
    got = _run_land(tmp_path, args, plans, [{"plan": "docs/plans/p0.md", "id": "census[1]"}, {"plan": "docs/plans/p9.md", "id": "census[1]"}])
    assert got["r"]["landPaths"] == ["docs/plans/p0.md", "docs/plans/p9.md", "state/mise-workflows/r.seam/p0.txt"]
    land = [c for c in got["calls"] if c[0] == "land-residue"]
    assert len(land) == 1
    assert '/bin/coordinator-invoke" ceremony.commit_v2 --params-file REPO/scratch/mise-land-' in land[0][1]
    assert "untouched.md" not in land[0][1]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_land_stage_declares_every_path_past_sixteen(tmp_path):
    args = _args(batches=1)
    template = args["batches"][0]["plans"][0]
    args["batches"][0]["plans"] = [
        {**template, "plan": f"docs/plans/q{i}.md", "seamRef": f"state/mise-workflows/r.seam/q{i}.txt"}
        for i in range(20)
    ]
    got = _run_land(tmp_path, args, [], [{"plan": "docs/plans/p0.md", "id": "census[1]"}])
    expected = ["docs/plans/p0.md"] + [f"state/mise-workflows/r.seam/q{i}.txt" for i in range(20)]
    assert got["r"]["landPaths"] == expected
    land = [c for c in got["calls"] if c[0] == "land-residue"]
    assert len(land) == 1
    assert all(p in land[0][1] for p in expected)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_land_stage_skipped_when_union_is_empty(tmp_path):
    got = _run_land(tmp_path, _args(apm_items=False, batches=2), [_plan_row("docs/plans/p0.md")], [])
    assert got["r"]["landPaths"] == []
    assert got["r"]["landed"] is None
    assert not [c for c in got["calls"] if c[0] == "land-residue"]
