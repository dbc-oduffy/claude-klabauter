"""Every halt exit of the ask script leaves a terminal stage and a halt record.

Compose tests (1)-(2) evaluate the composed halt-record guard in node against the args a resume
refire carries (no `runToken`; a launcher child's first script holds a null inline slot). Backstop
tests (3)-(4) drive `returned_halt.land_returned_halt` on a tmp_path record dir. Each test is a
strict xfail until the row that delivers it removes the marker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_roadmap import NO_READY_LITERAL
from coordinator_core.ops.dispatch_emit.returned_halt import land_returned_halt
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_SIZING = "state/sizings/x.yaml"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_COMMIT_CALL = "try { _haltCommit = await _commitHaltRecord(); }"
_REFIRE_ARGS = {"sizing": _SIZING}
_PULL_HALT = {"halted": "refusal", "reason": NO_READY_LITERAL, "sizing": _SIZING, "run_id": "run-1"}


def _compose(monkeypatch, *, launcher_child: bool) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, launcher_child=launcher_child, **REVIEW_KW,
    )


def _halt_record_guard(script: str) -> str:
    """The condition of the `if` that opens the halt-record commit."""
    at = script.index(_COMMIT_CALL)
    opener = script[:at].rstrip().rsplit("\n", 2)[-2]
    match = re.fullmatch(r"\s*if \((.*)\) \{", opener)
    assert match, opener
    return match.group(1)


def _token_decl(script: str) -> str:
    match = re.search(r"^[ \t]*const _RUN_TOKEN = .*;$", script, re.MULTILINE)
    assert match
    return match.group(0)


def _guard_opens_on_refire(script: str) -> bool:
    """True when the composed halt-record guard is true for a pull halt reached by a resume refire."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    code = (
        f"const args = {json.dumps(_REFIRE_ARGS)}; const _STAGED_INLINE = null; const _commit = null;\n"
        f"const _halted = {json.dumps(_PULL_HALT)};\n"
        f"{_token_decl(script)}\n"
        f"console.log(JSON.stringify(!!({_halt_record_guard(script)})));\n"
    )
    out = subprocess.run(
        [node, "-e", code], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _assert_pull_halt_is_recorded(script: str) -> None:
    pull = script.index(NO_READY_LITERAL)
    ret = script.rindex("return { arm:")
    assert pull < script.index(_COMMIT_CALL) < ret
    assert _guard_opens_on_refire(script)


@pytest.mark.xfail(strict=True)
def test_m_plus_pull_halt_composes_the_halt_record_commit_before_the_terminal_return(monkeypatch):
    _assert_pull_halt_is_recorded(_compose(monkeypatch, launcher_child=False))


@pytest.mark.xfail(strict=True)
def test_launcher_child_pull_halt_composes_the_halt_record_commit_with_a_resolvable_token(monkeypatch):
    _assert_pull_halt_is_recorded(_compose(monkeypatch, launcher_child=True))


def _record_dir(tmp_path: Path, stage: str = "minted") -> Path:
    rd = tmp_path / "state" / "warp" / "run-1"
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(
        json.dumps({"stage": stage, "started_at": "2026-11-01T23:00:00Z", "session_id": None}), encoding="utf-8"
    )
    return rd


def _refusal_result() -> dict:
    return {"halted_by": dict(_PULL_HALT), "halt_commit": None, "sizing": _SIZING, "run_id": "run-1"}


@pytest.mark.xfail(strict=True)
def test_a_returned_refusal_halt_stamps_the_stage_halted_and_writes_the_marker(tmp_path):
    rd = _record_dir(tmp_path)
    marker = land_returned_halt(rd, _refusal_result(), launched_at=time.time() - 60)
    assert marker == rd / "halted.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["halted"] == "refusal"
    assert json.loads((rd / "run-stage.json").read_text(encoding="utf-8"))["stage"] == "halted"


@pytest.mark.xfail(strict=True)
def test_a_halt_marker_newer_than_the_launch_is_left_untouched(tmp_path):
    rd = _record_dir(tmp_path)
    marker = rd / "halted.json"
    marker.write_text(json.dumps({"status": "halted", "halted": "commit"}), encoding="utf-8")
    before = marker.read_bytes()
    launched_at = os.path.getmtime(marker) - 60
    assert land_returned_halt(rd, _refusal_result(), launched_at=launched_at) is None
    assert marker.read_bytes() == before
    assert json.loads((rd / "run-stage.json").read_text(encoding="utf-8"))["stage"] == "minted"
