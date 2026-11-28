"""A sizing whose `plan:` names a differently named plan keeps that plan across a stage-wait: the plan author
scaffolds at the cited path (never the slug path `coordinator-doc-new` refuses), and a stage-wait that the hold
cannot clear ends the run as a parked halt, never a refusal. Runs the composed script under node."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script, sizing_cited_plan
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, PARKED_REPLY_KEY
from coordinator_core.ops.dispatch_emit.tests import test_ask_compose_stage_handle as handle
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import stage_root  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIZING = "state/sizings/2026-11/2026-11-24-ue-research-requires-addon-corpus-1d7396.yaml"
_CITED = "docs/plans/2026-11/2026-11-24-hnd-ue-research-requires-addon-cor-048922.md"
_SLUG = "docs/plans/2026-11/2026-11-24-ue-research-requires-addon-corpus-1d7396.md"
_WAIT = {PARKED_REPLY_KEY: "stage-wait", "plan_path": _CITED, "waiting": {"blockers": ["ask-blocker-1"], "deadline": 1789000000}}


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, cited: str | None) -> Path:
    if cited:
        (tmp_path / cited).parent.mkdir(parents=True)
        (tmp_path / cited).write_text("# plan\n", encoding="utf-8")
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    monkeypatch.setattr(ask_compose, "load_sizing", lambda *_: {"plan": cited} if cited else {})
    return tmp_path


def _script(root: Path) -> str:
    return compose_ask_script(
        repo_root=str(root), prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (handle._BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, replies: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate = {"arm": "s", "halt": None, "baton": None, "resume_plan": None, "resume_rows": None, "tshirt": "S",
            "route": "spec-dispatch"}
    cfg = {
        "token": handle._TOKEN, "verify": {"items": handle._MET["items"]}, "commit": handle._LANDED,
        "replies": {"gate": gate, "gate-stage": gate, **replies}, **handle.readers_cfg(),
    }
    proc = subprocess.run(
        [node, "-"], input=handle._NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": handle.payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_plan_author_scaffolds_at_the_sizings_cited_plan_and_a_stage_wait_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(_repo(tmp_path, monkeypatch, cited=_CITED))
    out = _run(script, {"plan": {"plan_rel": _CITED, "stage_reply": _WAIT},
                        "blitz-stage-read": {"stage_reply": _WAIT}})
    author = next(c["prompt"] for c in out["calls"] if c["label"] == "plan")
    assert f"--out {_CITED}`" in author
    assert _SLUG not in author
    halt = out["result"]["halted_by"]
    assert halt["halted"] == HALT_PARKED, halt
    assert "ask-blocker-1" in halt["reason"] and "stage refused" not in str(halt)


def test_a_sizing_with_no_plan_edge_still_scaffolds_at_the_slug_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path, monkeypatch, cited=None)
    out = _run(_script(root), {"plan": {"plan_rel": _SLUG, "stage_reply": _WAIT}, "blitz-stage-read": {"stage_reply": _WAIT}})
    author = next(c["prompt"] for c in out["calls"] if c["label"] == "plan")
    assert f"--out {_SLUG}`" in author
    assert _CITED not in author


def test_a_cited_plan_missing_from_disk_is_not_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ask_compose, "load_sizing", lambda *_: {"plan": _CITED})
    assert sizing_cited_plan(str(tmp_path), _SIZING) is None
    assert sizing_cited_plan(str(tmp_path), None) is None
