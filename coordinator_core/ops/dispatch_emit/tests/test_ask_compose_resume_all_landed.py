"""A resume whose rows have all landed goes on to verify and commit instead of halting on an empty stage."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_STALE_REASON = "resume staged no dispatchable rows"
_LANDED_PREFIX = "  if (!_manifest.error && !(_manifest.rows ?? []).length && Array.isArray(_gate.resume_rows)"
_HALT_PREFIX = "  else if (!_parked && (_manifest.error || (!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger))) {"

_HARNESS = """
let _halted = null, _allLanded = false, _resumeLedger = false, _parked = null, _planRel = 'p.md', _sizingRel = 's.yaml', _runId = 'r';
let _gate = %s, _manifest = %s;
%s
console.log(JSON.stringify({ halted: _halted, allLanded: _allLanded, arm: _gate.arm }));
"""


def _lines(script: str) -> list[str]:
    return [ln for ln in script.split("\n") if ln.startswith((_LANDED_PREFIX, _HALT_PREFIX))]


def _drive(script: str, gate: dict, manifest: dict) -> dict:
    body = "\n".join(ln.replace("  else if (", "  if (", 1) for ln in _lines(script))
    program = _HARNESS % (json.dumps(gate), json.dumps(manifest), body)
    out = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


def test_the_refusal_halt_for_an_all_landed_resume_is_gone():
    script = _compose()
    assert _STALE_REASON not in script
    assert len(_lines(script)) == 2
    assert script.index(_LANDED_PREFIX) < script.index(_HALT_PREFIX) < script.index("label: 'verify'")
    assert [f for f in run_checks(script) if f.severity is Severity.ERROR] == []


def test_the_landed_resume_takes_the_xs_tail_so_the_review_wave_is_skipped():
    landed = _lines(_compose())[0]
    assert "_allLanded = true; _gate = { ..._gate, arm: 'xs' }" in landed


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_resume_with_only_gated_rows_does_not_halt_and_takes_the_xs_tail():
    script = _compose()
    manifest = {"rows": [], "gated": [{"id": "WC1"}], "owed": []}
    got = _drive(script, {"arm": "s", "resume_plan": "p.md", "resume_rows": ["C1"]}, manifest)
    assert got == {"halted": None, "allLanded": True, "arm": "xs"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
def test_resume_with_only_owed_rows_does_not_halt():
    script = _compose()
    manifest = {"rows": [], "gated": [], "owed": [{"id": "M1", "reason": "memo-send"}]}
    got = _drive(script, {"arm": "s", "resume_rows": []}, manifest)
    assert got["halted"] is None and got["allLanded"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "gate, manifest",
    [
        ({"arm": "s", "resume_rows": None}, {"rows": [], "gated": [{"id": "WC1"}], "owed": []}),
        ({"arm": "s", "resume_rows": ["C1"]}, {"rows": [], "gated": [], "owed": []}),
        ({"arm": "s", "resume_rows": ["C1"]}, {"error": "stage failed", "rows": [], "gated": [{"id": "G"}]}),
    ],
    ids=["fresh-run-zero-rows", "resume-nothing-owed-or-gated", "stage-error"],
)
def test_a_fresh_empty_stage_and_a_stage_error_still_halt(gate, manifest):
    got = _drive(_compose(), gate, manifest)
    assert got["halted"]["halted"] == "refusal" and got["allLanded"] is False
    assert got["arm"] == "s"
