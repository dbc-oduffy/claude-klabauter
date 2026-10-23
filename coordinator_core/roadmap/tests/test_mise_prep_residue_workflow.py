"""The mise-prep residue workflow source binds, validates, and keeps its agent contract.

Binds with hand-made args; no test reaches it through `mise-prep-run`, which does not emit it yet.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.workflow_bind import _workflow_bind_args
from coordinator_core.ops.workflow_validate import _workflow_validate
from coordinator_core.roadmap.mise_residue import RESIDUE_AGENT_TYPE

SOURCE = Path(__file__).resolve().parents[1] / "workflows" / "mise-prep-residue.mjs"

RESIDUE_TYPE = RESIDUE_AGENT_TYPE
APM_TYPE = "coordinator:apm"


def _args(*, apm_items: bool = True, batches: int = 2) -> dict:
    return {
        "schema": "mise-prep-residue-args/4",
        "repoRoot": "REPO",
        "runId": "20261019T1630Z-deadbeef",
        "rulingsCommand": "mise-prep-run --repo-root REPO --apply-rulings",
        "gateCommand": "mise-prep-gate --repo-root REPO",
        "residue": {"agentType": RESIDUE_TYPE, "model": "m-residue"},
        "apm": {"agentType": APM_TYPE, "model": "m-apm"},
        "batches": [
            {
                "id": f"b{i + 1}",
                "plans": [
                    {
                        "plan": f"docs/plans/p{i}.md",
                        "bar": ["bar: capabilities undeclared"],
                        "seam": ["seam: writes-collision a.py"],
                        "census": [{"index": 0, "command": "ls $(pwd)", "refusal": "subshell"}],
                    }
                ],
            }
            for i in range(batches)
        ],
        "apmItems": (
            [
                {
                    "kind": "census-leftover",
                    "id": "census[1]",
                    "plan": "docs/plans/p0.md",
                    "index": 1,
                    "question": "q",
                    "command": "node x.js",
                    "refusal": "node",
                },
                {"kind": "seam-fix-cycle", "id": "cycle:p", "plans": ["a", "b"], "path": "p", "detail": "d"},
            ]
            if apm_items
            else []
        ),
    }


def _bind(tmp_path: Path, args: dict) -> Path:
    reply = _workflow_bind_args({"script_path": str(SOURCE), "args": args})
    out = tmp_path / "bound.mjs"
    out.write_text(reply["script"], encoding="utf-8", newline="\n")
    return out


def test_bound_script_validates_ok(tmp_path):
    bound = _bind(tmp_path, _args())
    reply = _workflow_validate({"script_path": str(bound)})
    assert reply["ok"] is True, reply["findings"]
    assert reply["error_count"] == 0


def test_source_has_residue_apm_and_persist_calls_typed_from_args():
    text = SOURCE.read_text(encoding="utf-8")
    assert len(re.findall(r"\bagent\s*\(", text)) == 4
    assert "Write the ruling to the plan" not in text
    assert text.count("await phase('Persist')") == 1
    assert "agentType: args.residue.agentType" in text
    assert "model: args.residue.model" in text
    assert "agentType: args.apm.agentType" in text
    assert "model: args.apm.model" in text
    assert "args.batches" in text and ".map(" in text


def test_source_carries_no_model_literal_and_no_top_level_args_declaration():
    text = SOURCE.read_text(encoding="utf-8")
    assert not re.search(r"model\s*:\s*['\"]", text)
    assert not re.search(r"^(const|let|var) args\b", text, re.M)


def test_bound_literal_round_trips_the_args(tmp_path):
    args = _args()
    reply = _workflow_bind_args({"script_path": str(SOURCE), "args": args})
    assert reply["bound_keys"] == sorted(args)
    start = reply["script"].index("const args = ") + len("const args = ")
    literal = reply["script"][start : reply["script"].index("\n}\n", start) + 2]
    assert json.loads(literal) == args


def test_plan_block_renders_seam_reference_only_when_set():
    text = SOURCE.read_text(encoding="utf-8")
    assert "if (p.seamRef)" in text
    assert "' writes-collision line(s) in ' + p.seamRef + ' -- read that file; each line is one seam finding'" in text
    assert "p.seamRefLines" in text


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_member_with_seam_ref_renders_read_that_file_line(tmp_path):
    args = _args(apm_items=False, batches=2)
    args["batches"][0]["plans"][0].update(seamRef="state/mise-workflows/r.seam/p0.txt", seamRefLines=3)
    bound = _bind(tmp_path, args)
    body = bound.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    harness = tmp_path / "run.cjs"
    harness.write_text(
        "const prompts = [];\n"
        "const agent = async (p, o) => { prompts.push(p);\n"
        "  if (o.label === 'land-residue') return { committed: true, sha: 'abc1234', error: null };\n"
        "  return { plans: [] }; };\n"
        "const phase = async () => {};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;\n"
        f"const body = {json.dumps(body)};\n"
        "new AsyncFunction('agent', 'phase', body)(agent, phase).then(() => {\n"
        "  console.log(JSON.stringify(prompts));\n"
        "});\n",
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    prompts = json.loads(out.stdout.strip().splitlines()[-1])
    line = "seam: 3 writes-collision line(s) in state/mise-workflows/r.seam/p0.txt -- read that file"
    assert sum(line in p for p in prompts) == 1
    assert sum("writes-collision line(s) in" in p for p in prompts) == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_land_failure_throws(tmp_path):
    args = _args(apm_items=False, batches=2)
    args["batches"][0]["plans"][0].update(seamRef="state/mise-workflows/r.seam/p0.txt", seamRefLines=3)
    bound = _bind(tmp_path, args)
    body = bound.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    harness = tmp_path / "run.cjs"
    harness.write_text(
        "const agent = async (p, o) => {\n"
        "  if (o.label === 'land-residue') return { committed: false, sha: null, error: 'refused' };\n"
        "  return { plans: [] }; };\n"
        "const phase = async () => {};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;\n"
        f"const body = {json.dumps(body)};\n"
        "new AsyncFunction('agent', 'phase', body)(agent, phase).then(() => {\n"
        "  console.log('RESOLVED');\n"
        "}, (e) => { console.error('REJECTED: ' + e.message); process.exit(3); });\n",
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 3
    assert "land-residue did not commit" in out.stderr


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_member_renders_spine_lines_and_step0_census_line(tmp_path):
    args = _args(apm_items=False, batches=1)
    member = args["batches"][0]["plans"][0]
    member["spine"] = ["spine-repair: tests-broad-suite -- name the file"]
    member["census"] = [
        {"index": 0, "command": "python -c 'x'", "refusal": "inline python", "class": "step0"},
        {"index": 1, "command": "ls $(pwd)", "refusal": "subshell", "class": "rewrite"},
    ]
    bound = _bind(tmp_path, args)
    body = bound.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    harness = tmp_path / "run.cjs"
    harness.write_text(
        "const prompts = [];\n"
        "const agent = async (p, o) => { prompts.push(p); return { plans: [] }; };\n"
        "const phase = async () => {};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;\n"
        f"const body = {json.dumps(body)};\n"
        "new AsyncFunction('agent', 'phase', body)(agent, phase).then(() => {\n"
        "  console.log(JSON.stringify(prompts));\n"
        "});\n",
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    prompt = json.loads(out.stdout.strip().splitlines()[-1])[0]
    assert "  spine-repair: tests-broad-suite -- name the file" in prompt
    assert "census[0] moves into its row's step 0: python -c 'x'  (refused: inline python)" in prompt
    assert "census[1] needs a rewrite: ls $(pwd)  (refused: subshell)" in prompt


def test_source_names_residue_author_and_args_v4():
    text = SOURCE.read_text(encoding="utf-8")
    assert "coordinator:residue-author" in text
    assert "mise-prep-residue-args/4" in text
    assert "beyond the named classes and spine repairs above" in text


def test_apm_call_is_guarded_by_non_empty_items():
    text = SOURCE.read_text(encoding="utf-8")
    guard = text.index("apmItems.length > 0")
    assert guard < text.index("agent(", text.index("await phase('APM')"))
    assert "returnedLeftovers" in text


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("apm_items,expect_apm", [(True, 1), (False, 0)])
def test_run_with_stubbed_agent_counts_calls(tmp_path, apm_items, expect_apm):
    bound = _bind(tmp_path, _args(apm_items=apm_items))
    body = bound.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    harness = tmp_path / "run.cjs"
    harness.write_text(
        "const calls = [];\n"
        "const agent = async (p, o) => { calls.push(o); return o.label === 'apm-rulings' "
        "? { rulings: [] } : { plans: [] }; };\n"
        "const phase = async () => {};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;\n"
        f"const body = {json.dumps(body)};\n"
        "new AsyncFunction('agent', 'phase', body)(agent, phase).then(() => {\n"
        "  console.log(JSON.stringify(calls.map((c) => [c.agentType, c.model])));\n"
        "});\n",
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    calls = json.loads(out.stdout.strip().splitlines()[-1])
    residue = [c for c in calls if c[0] == RESIDUE_TYPE]
    apm = [c for c in calls if c[0] == APM_TYPE]
    assert len(residue) == 2 and all(c[1] == "m-residue" for c in residue)
    assert len(apm) == expect_apm and all(c[1] == "m-apm" for c in apm)


def _run_persist(tmp_path: Path, rulings: list, applied: list) -> subprocess.CompletedProcess:
    bound = _bind(tmp_path, _args(batches=1))
    body = bound.read_text(encoding="utf-8").replace("export const meta", "const meta", 1)
    harness = tmp_path / "run.cjs"
    harness.write_text(
        "const calls = [];\n"
        "const agent = async (p, o) => { calls.push([o.label, o.agentType, o.model, p]);\n"
        "  if (o.label === 'apm-rulings') return { rulings: " + json.dumps(rulings) + " };\n"
        "  if (o.label === 'persist-rulings') return { applied: " + json.dumps(applied) + ", refused: [] };\n"
        "  return { plans: [] }; };\n"
        "const phase = async () => {};\n"
        "const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;\n"
        f"const body = {json.dumps(body)};\n"
        "new AsyncFunction('agent', 'phase', body)(agent, phase).then((r) => {\n"
        "  console.log(JSON.stringify({ calls, r }));\n"
        "}, (e) => { console.error('REJECTED: ' + e.message); process.exit(3); });\n",
        encoding="utf-8",
        newline="\n",
    )
    return subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


_RULED = [
    {"item": "census[1]", "ruling": "drop", "plan": "docs/plans/p0.md", "pmOnly": False},
    {"item": "cycle:p", "ruling": "order", "plan": "a", "pmOnly": True},
]
_STRAY = {"item": "census[9]", "ruling": "x", "plan": "docs/plans/p0.md", "pmOnly": False}


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_persist_resolves_when_scribe_confirms_every_ruled_id_and_drops_unmatched(tmp_path):
    out = _run_persist(tmp_path, _RULED + [_STRAY], ["census[1]", "cycle:p"])
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    scribe = [c for c in got["calls"] if c[0] == "persist-rulings"]
    assert len(scribe) == 1 and scribe[0][1:3] == [RESIDUE_TYPE, "m-residue"]
    assert "mise-prep-run --repo-root REPO --apply-rulings REPO/scratch/mise-rulings-20261019T1630Z-deadbeef.json" in scribe[0][3]
    assert "census[9]" not in scribe[0][3]
    assert got["r"]["persisted"] == ["census[1]", "cycle:p"]
    assert [u["item"] for u in got["r"]["unmatched"]] == ["census[9]"]


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("applied", [[], ["census[1]"]])
def test_persist_rejects_naming_missing_ids_when_confirmation_is_short(tmp_path, applied):
    out = _run_persist(tmp_path, _RULED, applied)
    assert out.returncode == 3
    assert "REJECTED" in out.stderr and "cycle:p" in out.stderr


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_no_scribe_runs_when_no_ruling_matches_an_item(tmp_path):
    out = _run_persist(tmp_path, [_STRAY], [])
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert not [c for c in got["calls"] if c[0] == "persist-rulings"]
    assert len(got["r"]["unmatched"]) == 1
