"""A plan-scoped stop rule (only `_haltedPlans` set, `_halted` null) still ends in a typed stop_rule halt."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_LIFT = "  if (!_halted && _stoppedBy.length) {\n"
_CLOSE = "stopped_rows: _stoppedRows }; }\n"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, stopped: list, halted, plan_reasons: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    start = script.index(_LIFT)
    wrap = script[start : script.index(_CLOSE, start) + len(_CLOSE)]
    prog = (
        "(async () => {\n"
        f"const _stoppedBy = {json.dumps(stopped)}; const _haltedByHold = false;\n"
        f"let _halted = {json.dumps(halted)}; let _consumedReturns = {{}};\n"
        f"const _haltedPlanReasons = new Map(Object.entries({json.dumps(plan_reasons)}));\n"
        "const _rows = {}; for (const k of " + json.dumps(stopped) + ") _rows[k] = Promise.resolve('STOP-RULE-FIRED');\n"
        "const _sizingRel = 's.yaml', _runId = 'r';\n"
        + wrap
        + "\nconsole.log(JSON.stringify({ halted: _halted }));\n})();"
    )
    proc = subprocess.run(
        [node, "-e", prog], capture_output=True, text=True, encoding="utf-8", timeout=60, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.splitlines()[-1])


def test_a_plan_scoped_stop_rule_becomes_the_typed_stop_rule_halt(monkeypatch):
    out = _run(_script(monkeypatch), ["C1"], None, {"docs/plans/p.md": "C1"})
    assert out["halted"]["halted"] == "stop_rule" and out["halted"]["stopped_rows"] == ["C1"]
    assert out["halted"]["reason"].startswith("stop rule fired on C1")


def test_a_seam_only_plan_halt_is_not_lifted(monkeypatch):
    out = _run(_script(monkeypatch), [], None, {"docs/plans/p.md": "seam drift at wave 1"})
    assert out["halted"] is None


def test_a_seam_reason_is_skipped_for_the_stop_rule_reason(monkeypatch):
    out = _run(
        _script(monkeypatch), ["C2"], None, {"a.md": "seam drift at wave 1", "b.md": "C2"}
    )
    assert out["halted"]["reason"].startswith("stop rule fired on C2")


def test_a_stopped_row_whose_plan_already_holds_a_seam_reason_still_halts(monkeypatch):
    out = _run(_script(monkeypatch), ["C3"], None, {"a.md": "seam drift at wave 1"})
    assert out["halted"]["halted"] == "stop_rule" and out["halted"]["stopped_rows"] == ["C3"]


def test_an_existing_global_halt_is_kept(monkeypatch):
    out = _run(_script(monkeypatch), ["C1"], "C1", {"a.md": "C9"})
    assert out["halted"]["reason"].startswith("stop rule fired on C1")


def test_the_lift_precedes_the_halt_record_commit(monkeypatch):
    script = _script(monkeypatch)
    assert script.index(_LIFT) < script.index("await _commitHaltRecord()")
