"""The residue workflow's land stage, run under a real node process (cadence tier)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.roadmap.tests.test_mise_prep_residue_workflow import _args, _bind

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH"),
]


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


def _land_command(prompt: str) -> str:
    return next(line for line in prompt.splitlines() if "ceremony.commit_v2" in line)


def test_land_stage_takes_only_the_union_deduplicated(tmp_path):
    args = _args(batches=2)
    args["batches"][0]["plans"][0]["seamRef"] = "state/mise-workflows/r.seam/p0.txt"
    plans = [_plan_row("docs/plans/p0.md", declared=["x"]), _plan_row("docs/plans/untouched.md")]
    got = _run_land(tmp_path, args, plans, [{"plan": "docs/plans/p0.md", "id": "census[1]"}, {"plan": "docs/plans/p9.md", "id": "census[1]"}])
    assert got["r"]["landPaths"] == ["docs/plans/p0.md", "docs/plans/p9.md", "state/mise-workflows/r.seam/p0.txt"]
    land = [c for c in got["calls"] if c[0] == "land-residue"]
    assert len(land) == 1
    assert "--params-file" not in land[0][1]
    assert "untouched.md" not in land[0][1]
    from coordinator_core.bash_guards import block_subagent_commit as guard

    cmd = _land_command(land[0][1])
    assert guard._resolve_git_commit_agent_pathspec(cmd) == (got["r"]["landPaths"], False)
    refused = cmd.replace(cmd[cmd.index("ceremony.commit_v2") + 19:cmd.index(" --repo")], "--params-file REPO/p.json")
    assert guard._resolve_git_commit_agent_pathspec(refused) is None


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


def test_land_stage_skipped_when_union_is_empty(tmp_path):
    got = _run_land(tmp_path, _args(apm_items=False, batches=2), [_plan_row("docs/plans/p0.md")], [])
    assert got["r"]["landPaths"] == []
    assert got["r"]["landed"] is None
    assert not [c for c in got["calls"] if c[0] == "land-residue"]
