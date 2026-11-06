"""ask_compose: an indeterminate (-32004) sizing-assemble is reconciled by the scout, halts resumably when the
sizing is unconfirmed, and every refusal halt on a {sizing} submission names a {sizing} refire."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit.ask_compose import (
    SIZING_ASSEMBLE_INDETERMINATE_RE,
    _lit,
    _refire_js,
    compose_ask_script,
    sizing_assemble_reconcile_clause,
)
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import REVERIFY_INDETERMINATE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.warp_fire import fire_command

_SIZING = "state/sizings/x.yaml"
_INDETERMINATE = (
    "warm dispatch indeterminate: door delivered this request to the warm engine and did not get back a response"
)


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over):
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def _scout_halt_lines(script: str) -> list[str]:
    lines = script.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("if (!_scoutAssembled)"))
    return lines[start:start + 4]


def _run_halt(scouted: dict, tmp_path) -> dict:
    """Runs the composed scout-result halt chain over `scouted`; returns {halted, sizing_rel}."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = _compose()
    refire_cmd = next(ln for ln in script.splitlines() if "const _refireCmd = " in ln)
    prog = (
        "const REPO_ROOT = 'REPO'; const _sizingPath = 'REPO/state/sizings/x.yaml'; const _runId = 'run-1';\n"
        "const _boundRelOf = () => null; let _sizingRel = null; let _halted = null; const _scoutAssembled = true;\n"
        + refire_cmd + "\n"
        f"const _scouted = {json.dumps(scouted)};\n"
        + "\n".join(_scout_halt_lines(script))
        + "\nconsole.log(JSON.stringify({ halted: _halted, sizing_rel: _sizingRel }));\n"
    )
    f = tmp_path / "t.js"
    f.write_text(prog, encoding="utf-8")
    out = subprocess.run(
        ["node", str(f)], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return json.loads(out)


def test_the_scout_prompt_carries_the_assemble_reconcile_clause_for_its_sizing_path():
    script = _compose()
    assert "A -32004 is never itself the assemble" in script
    assert "retry it only as the -32004 clause" in script
    clause = sizing_assemble_reconcile_clause(_SIZING)
    assert _SIZING in clause and "exit_code 0" in clause


def test_the_indeterminate_kind_is_an_existing_mechanical_kind_not_a_refusal():
    assert REVERIFY_INDETERMINATE != "refusal"
    assert hrc.HALT_REMEDY[REVERIFY_INDETERMINATE][0] == hrc.MECHANICAL


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_an_unconfirmed_indeterminate_assemble_halts_resumably_with_an_ask_refire(field, tmp_path):
    scouted = {"exit_code": 1, "stdout": "", "stderr": "", "sizing_present": False}
    scouted[field] = "error code -32004 " + _INDETERMINATE
    out = _run_halt(scouted, tmp_path)
    halted = out["halted"]
    assert halted["halted"] == REVERIFY_INDETERMINATE != "refusal"
    assert halted["run_id"] == "run-1" and "-32004" in halted["detail"]
    assert halted["next_action"] == "Refire: " + fire_command(None)
    assert out["sizing_rel"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_indeterminate_assemble_with_the_sizing_on_disk_refires_with_the_sizing(tmp_path):
    scouted = {"exit_code": 1, "stdout": _INDETERMINATE, "stderr": "", "sizing_present": True}
    out = _run_halt(scouted, tmp_path)
    assert out["halted"]["halted"] == REVERIFY_INDETERMINATE
    assert out["sizing_rel"] == _SIZING
    assert out["halted"]["next_action"] == "Refire: " + fire_command(_SIZING)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_plain_assemble_failure_still_halts_as_a_refusal(tmp_path):
    out = _run_halt({"exit_code": 2, "stdout": "bad exit_criterion", "stderr": "", "sizing_present": False}, tmp_path)
    assert out["halted"]["halted"] == "refusal"
    assert out["halted"]["reason"].startswith("sizing-assemble failed (exit 2)")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_exit_zero_reply_naming_the_marker_is_not_halted_by_this_branch(tmp_path):
    out = _run_halt({"exit_code": 0, "stdout": "-32004 reconciled", "stderr": "", "sizing_present": True}, tmp_path)
    assert out["halted"] is None


def test_the_marker_regex_matches_both_reply_shapes():
    assert SIZING_ASSEMBLE_INDETERMINATE_RE.startswith("/") and SIZING_ASSEMBLE_INDETERMINATE_RE.endswith("/i")
    assert "-32004" in SIZING_ASSEMBLE_INDETERMINATE_RE and "warm dispatch indeterminate" in SIZING_ASSEMBLE_INDETERMINATE_RE


def test_every_refusal_halt_site_on_a_sizing_submission_names_the_sizing_refire():
    script = _compose(prompt=None, sizing_rel=_SIZING)
    assert _refire_js(_SIZING) == _lit(fire_command(_SIZING))
    sizing_call = _lit(fire_command(_SIZING))
    staged = re.search(rf"const (_PF\d+) = {re.escape(sizing_call)};", script)
    operand = staged.group(1) if staged else sizing_call
    assert f"if (_halted.halted === 'refusal') return 'Refire: ' + {operand};" in script
    assert "_refireCmd" not in script
    assert 'args: {"ask"' not in script


def test_a_prompt_submission_falls_back_to_the_ask_refire_only_while_no_sizing_exists():
    script = _compose()
    assert "const _refireCmd = () => (_sizingRel ? " in script
    assert 'args: {"ask"' in script
