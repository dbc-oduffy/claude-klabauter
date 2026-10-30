"""A sizing refire of a run that halted at plan-blitz answers through the one-child launcher, never inline.

Drives `submit_engine_ask.evaluate` at default params over a tmp_path repo whose record dir holds the
halted run's `run.json` and `run-stage.json`, with and without the `halted.json` marker. The emit stub
fails the test if called; the hook's process time on the path is held under 500ms.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags


from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, write_halt_marker

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SESSION = "11111111-2222-3333-4444-555555555555"
_SIZING = "state/sizings/2026-10-29-plan-blitz-halt.yaml"
_PLAN = "docs/plans/2026-10-29-plan-blitz-halt.md"
_RUN = "warp-2026-10-29-plan-blitz-halt-20261029T084950-3413f6"
_BUDGET_S = 0.5


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    doc = {
        "schema": "sizing-object", "name": "plan-blitz halt", "intent": "refire after a plan-blitz halt",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized", "plan": _PLAN,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-plan-blitz-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-29", "mode": "pm"}},
        "requirement_register": {
            "sources": [{"doc": "ask", "ref": "ask"}],
            "rows": [{"id": "R1", "source_text": "refire", "surface": "internal", "status": "open"}],
        },
    }
    (repo / _SIZING).parent.mkdir(parents=True)
    (repo / _SIZING).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return repo


def _halt_at_plan_blitz(repo: Path, *, marker: bool) -> Path:
    rd = repo / RECORD_DIR_ROOT / _RUN
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": _SIZING}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "halted"}), encoding="utf-8")
    if marker:
        write_halt_marker(
            rd, halted="refusal", sizing_rel=_SIZING, plan_rel=_PLAN, reason="plan-blitz reported no ready plan"
        )
    return rd


def _payload(repo: Path) -> dict:
    return {
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SESSION,
        "transcript_path": str(repo / "transcript.jsonl"), "cwd": str(repo), "tool_use_id": "toolu_plan_blitz",
        "tool_input": {"name": "coordinator:engine-ask", "args": {"sizing": _SIZING}},
    }


@pytest.mark.parametrize("marker", [False, True], ids=["no-marker", "with-marker"])
def test_refire_after_a_plan_blitz_halt_takes_the_launcher_without_composing_inline(
    tmp_path, monkeypatch, record_property, marker
):
    from coordinator_core.hooks import submit_engine_ask as hook

    repo = _repo(tmp_path)
    rd = _halt_at_plan_blitz(repo, marker=marker)
    assert (rd / HALTED_FILE).exists() is marker
    if marker:
        body = json.loads((rd / HALTED_FILE).read_text(encoding="utf-8"))
        assert body[HALT_PLAN_FIELD] == _PLAN and body["halted"] == "refusal", body

    spawned, launched = [], []
    monkeypatch.setattr(hook, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)
    script = repo / "launcher.js"
    script.write_text("//", encoding="utf-8")

    def emit(params, root):
        pytest.fail("the hook composed inline on a sizing refire of an emitted run")

    def launch(root, parent_id, children, session_id):
        launched.append([c.run_id for c in children])
        return script

    started = time.process_time()
    env = hook.evaluate(_payload(repo), emit=emit, launch=launch)
    process_s = time.process_time() - started
    record_property("process_s", round(process_s, 3))

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    assert hso["updatedInput"]["scriptPath"] == str(script), env
    assert launched == [[_RUN]], launched
    assert spawned == [_RUN]
    assert process_s < _BUDGET_S, process_s
