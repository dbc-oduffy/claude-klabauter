"""A stop_rule halt on a plan-blitz chunk (not a `_rows` key) surfaces the chunk's own reply."""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.tests.test_stop_rule_halt_records_row_return import _script, _wrap
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

OBSERVED = (
    "PARTIAL: .coordinator-local/subagent-share/dispatch-reports/"
    "2026-11-13-hnd-lessons-grind-terminal-commit-dd6810/C1.md\n\n"
    'STOP-RULE-FIRED: "if minting and firing the prewritten params under tmp_path does NOT reproduce '
    "refused == 'run-token', stop and report the reproduction gap\". Minting and firing them under "
    "tmp_path commits with no run-token refusal, and the test file reports 4 passed at HEAD. "
    "C2's fix site is therefore not named by this reproduction."
)
REPORT = ".coordinator-local/subagent-share/dispatch-reports/2026-11-13-hnd-lessons-grind-terminal-commit-dd6810/C1.md"


def _run(wrap: str, stopped: list, halted, rows: dict, stopped_text: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = (
        "(async () => {\n"
        f"const _stoppedBy = {json.dumps(stopped)}; const _haltedByHold = false;\n"
        f"const _stoppedText = {json.dumps(stopped_text)};\n"
        f"let _halted = {json.dumps(halted)}; let _consumedReturns = {{}};\n"
        f"const _rows = {{}}; for (const [k, v] of Object.entries({json.dumps(rows)})) _rows[k] = Promise.resolve(v);\n"
        "const _sizingRel = 's.yaml', _runId = 'r';\n"
        + wrap
        + "\nconsole.log(JSON.stringify({ halted: _halted, consumed: _consumedReturns }));\n})();"
    )
    proc = subprocess.run(
        [node, "-e", prog], capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.splitlines()[-1])


def test_a_chunk_stop_records_its_return_and_names_clause_and_report_path(monkeypatch):
    out = _run(_wrap(_script(monkeypatch)), ["C1"], "C1", {}, {"C1": OBSERVED})
    reason = out["halted"]["reason"]
    assert "returned nothing" not in reason and "returned nothing" not in out["consumed"]["C1"]
    assert REPORT in reason and "STOP-RULE-FIRED" in reason and "reproduction gap" in reason
    assert out["consumed"]["C1"] == OBSERVED
    assert out["halted"]["stopped_rows"] == ["C1"]


def test_a_chunk_stop_with_no_text_still_says_returned_nothing(monkeypatch):
    out = _run(_wrap(_script(monkeypatch)), ["C1"], "C1", {}, {})
    assert out["halted"]["reason"] == "stop rule fired on C1: returned nothing"


def test_a_row_return_wins_over_chunk_text(monkeypatch):
    out = _run(_wrap(_script(monkeypatch)), ["C1"], "C1", {"C1": "BLOCKED: row"}, {"C1": OBSERVED})
    assert out["consumed"] == {"C1": "BLOCKED: row"}
    assert out["halted"]["reason"] == "stop rule fired on C1: BLOCKED: row"


def test_the_chunk_gate_keeps_the_stopped_text():
    src = inspect.getsource(emit)
    assert "_stoppedBy.push(id);\\n\"\n        \"      _stoppedText[id] = _text;" in src
    assert "const _stoppedText = {};" in src
