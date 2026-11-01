"""A refire whose plan rows are all resolved stages no rows and takes the ledger-closing tail, not the refusal halt."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_LANDED_PREFIX = "  if (!_manifest.error && !(_manifest.rows ?? []).length && Array.isArray(_gate.resume_rows)"
_HALT_PREFIX = "  else if (!_parked && (_manifest.error || (!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger))) {"

_HARNESS = """
let _halted = null, _allLanded = false, _xsTail = false, _resumeLedger = false, _parked = null, _planRel = 'p.md', _sizingRel = 's.yaml', _runId = 'r';
let _gate = %s, _manifest = %s;
%s
console.log(JSON.stringify({ halted: _halted, allLanded: _allLanded, xsTail: _xsTail }));
"""


def _drive(gate: dict, manifest: dict) -> dict:
    lines = [ln for ln in _compose().split("\n") if ln.startswith((_LANDED_PREFIX, _HALT_PREFIX))]
    body = "\n".join(ln.replace("  else if (", "  if (", 1) for ln in lines)
    program = _HARNESS % (json.dumps(gate), json.dumps(manifest), body)
    out = subprocess.run(
        ["node", "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_all_resolved_refire_with_empty_resume_rows_takes_the_ledger_tail():
    gate = {"arm": "s", "resume_plan": "p.md", "resume_rows": []}
    got = _drive(gate, {"rows": [], "gated": [], "owed": []})
    assert got == {"halted": None, "allLanded": True, "xsTail": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.parametrize(
    "gate, manifest",
    [
        ({"arm": "s", "resume_plan": "p.md", "resume_rows": ["C1"]}, {"rows": [], "gated": [], "owed": []}),
        ({"arm": "s", "resume_rows": None}, {"rows": [], "gated": [], "owed": []}),
        ({"arm": "s", "resume_rows": []}, {"error": "stage failed", "rows": []}),
    ],
    ids=["rows-owed-but-none-staged", "fresh-run-zero-rows", "stage-error"],
)
def test_other_empty_stages_and_stage_errors_still_halt(gate, manifest):
    got = _drive(gate, manifest)
    assert got["halted"]["halted"] == "refusal" and got["allLanded"] is False
