"""The residue workflow's persist stage, run under a real node process (cadence tier)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.roadmap.tests.test_mise_prep_residue_workflow import (
    RESIDUE_TYPE,
    _RULED,
    _STRAY,
    _args,
    _bind,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH"),
]


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


@pytest.mark.parametrize("applied", [[], ["census[1]"]])
def test_persist_rejects_naming_missing_ids_when_confirmation_is_short(tmp_path, applied):
    out = _run_persist(tmp_path, _RULED, applied)
    assert out.returncode == 3
    assert "REJECTED" in out.stderr and "cycle:p" in out.stderr


def test_no_scribe_runs_when_no_ruling_matches_an_item(tmp_path):
    out = _run_persist(tmp_path, [_STRAY], [])
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert not [c for c in got["calls"] if c[0] == "persist-rulings"]
    assert len(got["r"]["unmatched"]) == 1
