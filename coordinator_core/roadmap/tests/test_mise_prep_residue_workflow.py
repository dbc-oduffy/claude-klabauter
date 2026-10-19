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
        "schema": "mise-prep-residue-args/1",
        "repoRoot": "REPO",
        "runId": "20261019T1630Z-deadbeef",
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
                    "plan": "docs/plans/p0.md",
                    "index": 1,
                    "question": "q",
                    "command": "node x.js",
                    "refusal": "node",
                },
                {"kind": "seam-fix-cycle", "plans": ["a", "b"], "path": "p", "detail": "d"},
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


def test_source_has_one_residue_call_and_one_apm_call_typed_from_args():
    text = SOURCE.read_text(encoding="utf-8")
    assert len(re.findall(r"\bagent\s*\(", text)) == 2
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
