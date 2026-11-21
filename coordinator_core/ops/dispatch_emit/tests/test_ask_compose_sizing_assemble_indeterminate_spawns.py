"""ask_compose: an indeterminate (-32004) sizing-assemble is reconciled by the scout, halts resumably when the
sizing is unconfirmed, and every refusal halt on a {sizing} submission names a {sizing} refire."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import REVERIFY_INDETERMINATE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.warp_fire import fire_command
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIZING = "state/sizings/x.yaml"
_INDETERMINATE = (
    "warm dispatch indeterminate: door delivered this request to the warm engine and did not get back a response"
)


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over) -> str:
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


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.parametrize("field", ["stdout", "stderr"])
def test_an_unconfirmed_indeterminate_assemble_halts_resumably_with_an_ask_refire(field, tmp_path: Path) -> None:
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
def test_an_indeterminate_assemble_with_the_sizing_on_disk_refires_with_the_sizing(tmp_path: Path) -> None:
    scouted = {"exit_code": 1, "stdout": _INDETERMINATE, "stderr": "", "sizing_present": True}
    out = _run_halt(scouted, tmp_path)
    assert out["halted"]["halted"] == REVERIFY_INDETERMINATE
    assert out["sizing_rel"] == _SIZING
    assert out["halted"]["next_action"] == "Refire: " + fire_command(_SIZING)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_plain_assemble_failure_still_halts_as_a_refusal(tmp_path: Path) -> None:
    out = _run_halt({"exit_code": 2, "stdout": "bad exit_criterion", "stderr": "", "sizing_present": False}, tmp_path)
    assert out["halted"]["halted"] == "refusal"
    assert out["halted"]["reason"].startswith("sizing-assemble failed (exit 2)")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_exit_zero_reply_naming_the_marker_is_not_halted_by_this_branch(tmp_path: Path) -> None:
    out = _run_halt({"exit_code": 0, "stdout": "-32004 reconciled", "stderr": "", "sizing_present": True}, tmp_path)
    assert out["halted"] is None
