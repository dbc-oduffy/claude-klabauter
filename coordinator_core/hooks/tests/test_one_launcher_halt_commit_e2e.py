"""Exit criterion end to end through the default refire call (`{sizing}` only): refiring a run whose first
launcher is live starts no second launcher and the first launcher's halt params stay admitted by
`block_subagent_commit`; a refire of a non-live run leaves the old token admitted, and a token the refire never minted stays refused naming the superseding launcher script."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT
from coordinator_core.session import liveness
from coordinator_core.warm import live_runs

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-11-21-one-launcher-e2e.yaml"
RUN = "warp-2026-11-21-one-launcher-e2e-20261121T010101-abc123"
FIRST_LAUNCHER = "state/warp/first-launcher.workflow.mjs"
RUN_SCRIPT = f"{RUN_DIR_ROOT}/{RUN}.workflow.mjs"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-one-launcher-e2e",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-21", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    (rd / "halted.json").write_text(json.dumps({"status": "halted", "halted": "commit", "sizing_rel": SIZING_REL}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "commit"}), encoding="utf-8")
    return tmp_path


def _payload(root: Path) -> dict:
    return {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL}},
    }


def _transcript(tmp_path: Path, params: dict) -> str:
    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    block = chan.render_params_block(params)
    prompt = "\n".join("  " + ln for ln in ["Commit.", *block.split("\n")])
    f = d / "agent-a.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    return str(f)


def _present(root: Path, token: str, script: str):
    params = {"script_path": script, "run_id": RUN, "run_token": token, "halted_by": {"reason": "halt"}}
    typed = chan.params_path(root, RUN).as_posix()
    return guard._prompt_sourced_params_admit_leg(typed, str(root), _transcript(root, params))


def _stub_launch(root: Path, parent_id: str, children, session_id) -> Path:
    path = root / "scratch" / f"{parent_id}.workflow.mjs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// launcher\n", encoding="utf-8")
    return path


def _wire(monkeypatch: pytest.MonkeyPatch, *, live: bool | None) -> list:
    monkeypatch.setattr(hrr, "engine_sha", lambda: None)
    if live is not None:
        monkeypatch.setattr(live_runs, "run_is_live", lambda r, run_id, **k: live)
    spawned: list = []
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: spawned.append(run_id) or True)
    return spawned


def _log(root: Path) -> Path:
    return root / RECORD_DIR_ROOT / RUN / hrr.REFIRE_LOG_FILE


def test_a_live_runs_default_refire_starts_no_second_launcher_and_its_first_token_is_admitted(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    rd = root / RECORD_DIR_ROOT / RUN
    token = run_token.mint(root, RUN, RUN_SCRIPT)
    hrr.write_refire_record(rd, FIRST_LAUNCHER, "launcher", None, 1.0)
    before = _log(root).read_text(encoding="utf-8")
    spawned = _wire(monkeypatch, live=True)

    def boom(*a, **k):
        raise AssertionError("a live-run refire spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    launched: list = []

    def launch(*a, **k):
        launched.append(a)
        return _stub_launch(*a)

    env = mod.evaluate(_payload(root), launch=launch)

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert RUN in hso["permissionDecisionReason"] and FIRST_LAUNCHER in hso["permissionDecisionReason"]
    assert launched == [] and spawned == []
    assert _log(root).read_text(encoding="utf-8") == before
    assert len(_log(root).read_text(encoding="utf-8").splitlines()) == 1
    assert _present(root, token, RUN_SCRIPT) is None


def test_a_non_live_runs_refire_leaves_the_old_token_admitted(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    rd = root / RECORD_DIR_ROOT / RUN
    old_token = run_token.mint(root, RUN, RUN_SCRIPT)
    _wire(monkeypatch, live=False)

    env = mod.evaluate(_payload(root), launch=_stub_launch)

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    launcher = hrr.latest_launcher(rd)
    assert launcher is not None and launcher["kind"] == "launcher"
    assert _present(root, old_token, RUN_SCRIPT) is None


def test_a_token_the_refire_never_minted_is_still_refused_after_the_refire(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    run_token.mint(root, RUN, RUN_SCRIPT)
    _wire(monkeypatch, live=False)
    mod.evaluate(_payload(root), launch=_stub_launch)
    superseding = hrr.latest_launcher(root / RECORD_DIR_ROOT / RUN)["script_path"]

    leg = _present(root, "never-minted", RUN_SCRIPT)

    assert leg is not None and leg.startswith(guard._LEG_PARAMS_SUPERSEDED) and superseding in leg


def test_the_real_liveness_predicate_refuses_the_refire_of_a_live_run(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    rd = root / RECORD_DIR_ROOT / RUN
    (rd / "halted.json").unlink()
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING_REL}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({
        "stage": "staged", "session_id": "live-owner",
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    run_token.mint(root, RUN, RUN_SCRIPT)
    hrr.write_refire_record(rd, FIRST_LAUNCHER, "launcher", None, 1.0)
    monkeypatch.setattr(liveness, "session_live", lambda sid: sid == "live-owner")
    spawned = _wire(monkeypatch, live=None)
    assert live_runs.run_is_live(root, RUN)

    env = mod.evaluate(_payload(root), launch=_stub_launch)

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and f"Run {RUN} is live" in hso["permissionDecisionReason"]
    assert spawned == [] and len(_log(root).read_text(encoding="utf-8").splitlines()) == 1


def test_the_real_liveness_predicate_does_not_refuse_once_the_owner_session_is_gone(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    rd = root / RECORD_DIR_ROOT / RUN
    (rd / "halted.json").write_text(json.dumps({"status": "resumed", "sizing_rel": SIZING_REL}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({
        "stage": "commit", "session_id": "dead-owner",
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    run_token.mint(root, RUN, RUN_SCRIPT)
    monkeypatch.setattr(liveness, "session_live", lambda sid: False)
    _wire(monkeypatch, live=None)
    assert not live_runs.run_is_live(root, RUN)
