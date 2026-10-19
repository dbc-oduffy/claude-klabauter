"""A Workflow resumeFromRunId of a halted run revised after its halt is denied, naming the fresh fire."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from coordinator_core.hooks import block_workflow_foreign_emission as bwfe
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.warp_fire import fire_command

RUN_ID = "ask-20261019T162916-dfedf5"
SIZING = "state/sizings/2026-10-19-x.yaml"


def _setup(tmp_path, *, status="halted", revised=True, sizing=SIZING):
    script = tmp_path / "scratch" / "warp" / f"{RUN_ID}.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("// emitted\n", encoding="utf-8")
    sha = hashlib.sha256(script.read_bytes()).hexdigest()
    (script.parent / (script.name + ".emitted.json")).write_text(json.dumps({"sha256": sha}), encoding="utf-8")
    rec = tmp_path / "state" / "warp" / RUN_ID
    halted_run.write_halt_marker(rec, halted="x", sizing_rel=sizing)
    if revised:
        assert halted_run.mark_revised(rec, "2026-10-19T00:00:00Z")
    if status == "resumed":
        halted_run.mark_resumed(rec)
    return script


def _call(script, tmp_path, **extra):
    return bwfe._handler({
        "tool_name": "Workflow",
        "tool_input": {"scriptPath": str(script), **extra},
        "cwd": str(tmp_path),
    })


def test_resume_of_revised_halt_is_denied_naming_fresh_fire(tmp_path):
    script = _setup(tmp_path)
    hso = _call(script, tmp_path, resumeFromRunId=RUN_ID)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert fire_command(SIZING) in hso["permissionDecisionReason"]
    assert RUN_ID in hso["permissionDecisionReason"]


def test_missing_sizing_names_ask_fire(tmp_path):
    script = _setup(tmp_path, sizing=None)
    hso = _call(script, tmp_path, resumeFromRunId=RUN_ID)["hookSpecificOutput"]
    assert fire_command(None) in hso["permissionDecisionReason"]


def test_without_resume_is_unchanged(tmp_path):
    assert _call(_setup(tmp_path), tmp_path) == {}


def test_no_stamp_is_unchanged(tmp_path):
    assert _call(_setup(tmp_path, revised=False), tmp_path, resumeFromRunId=RUN_ID) == {}


def test_resumed_status_is_unchanged(tmp_path):
    assert _call(_setup(tmp_path, status="resumed"), tmp_path, resumeFromRunId=RUN_ID) == {}


def test_script_outside_scratch_warp_is_unchanged(tmp_path):
    script = _setup(tmp_path)
    other = tmp_path / "elsewhere" / script.name
    other.parent.mkdir()
    other.write_bytes(script.read_bytes())
    (other.parent / (other.name + ".emitted.json")).write_bytes(
        (script.parent / (script.name + ".emitted.json")).read_bytes()
    )
    assert _call(other, tmp_path, resumeFromRunId=RUN_ID) == {}


def test_changed_script_keeps_sha_deny(tmp_path):
    script = _setup(tmp_path)
    script.write_text("// edited\n", encoding="utf-8")
    hso = _call(script, tmp_path, resumeFromRunId=RUN_ID)["hookSpecificOutput"]
    assert "changed after emission" in hso["permissionDecisionReason"]


@pytest.mark.skipif(os.name != "nt", reason="backslash separators are native only on Windows")
def test_backslash_script_path_is_denied(tmp_path):
    script = _setup(tmp_path)
    hso = _call(str(script).replace("/", "\\"), tmp_path, resumeFromRunId=RUN_ID)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_backslash_path_parsed_without_os_dependence(tmp_path):
    _setup(tmp_path)
    from pathlib import Path

    reason = bwfe._resume_after_revise_reason(Path(str(tmp_path / "scratch" / "warp" / f"{RUN_ID}.workflow.mjs").replace("/", "\\")))
    if os.name == "nt":
        assert reason and fire_command(SIZING) in reason
    else:
        assert reason is None or fire_command(SIZING) in reason
