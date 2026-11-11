"""End to end: an engine-ask emitted with `stop_after=plan` stays parked through its chained `warp-` successor,
the halt-resume rebuild, a fan-launcher child and `dispatch.ask_stage`; no stage handle, no manifest.json and no
product file appear, and a successor that cannot park refuses. Every leg drives the production entry point."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage, fan_launcher as fl, halt_resume_build as hrb, halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_PARKED,
    OP_ASK_STAGE,
    PARK_SOURCE_REPLY_KEY,
    PARKED_REPLY_KEY,
    RUN_DIR_ROOT,
    STOP_AFTER_FILE,
    STOP_AFTER_KEY,
    STOP_AFTER_PLAN,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit, _refuse_non_ask_chain_parent
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN as STAGEABLE_PLAN, PLAN_REL as STAGEABLE_REL

REL = "state/sizings/2026-11-04-chained-slice.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_BASE = "a" * 40
_PARKED_TEXT = "parked after plan: stop_after=plan"
_SIZING_DOC = {
    "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
    "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
    "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    "interaction_mode": "pm", "deliverable_id": "dlv-chained-abc123",
    "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-04", "mode": "pm"}},
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_SIZING_DOC, sort_keys=False), encoding="utf-8", newline="\n")
    (tmp_path / STAGEABLE_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / STAGEABLE_REL).write_text(STAGEABLE_PLAN, encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)
    return tmp_path


def _script(reply: dict) -> str:
    return Path(reply["path"]).read_text(encoding="utf-8")


def _emit_ask(repo: Path) -> dict:
    reply = _dispatch_emit({"ask": "add a thing", STOP_AFTER_KEY: STOP_AFTER_PLAN, "target_root": str(repo)})
    assert "error" not in reply, reply
    return reply


def _chain_params_from(script: str, repo: Path, run_id: str) -> dict:
    """The params the emitted script's own `_chain_emit_params_js` literal sends the chained `dispatch.emit`."""
    start = script.index("{ ask: true, sizing_path: _sizingRel,")
    depth = 0
    for end in range(start, len(script)):
        depth += {"{": 1, "}": -1}.get(script[end], 0)
        if depth == 0:
            break
    literal = re.sub(r"\.\.\.\(.*?\? \{.*?\} : \{\}\)", "", script[start : end + 1])
    env = {"_sizingRel": REL, "_SESSION_ID": "11111111-2222-3333-4444-555555555555", "REPO_ROOT": str(repo), "_runId": run_id}
    params: dict = {}
    for key, raw in re.findall(r"(\w+): ([^,{}]+?)(?:,|\s*\}|$)", literal.strip()):
        raw = raw.strip()
        params[key] = True if raw == "true" else env[raw] if raw in env else raw.strip("'")
    return params


def _product_files(repo: Path) -> set:
    skip = {".git", "state", "scratch", ".coordinator-local"}
    return {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and p.relative_to(repo).parts[0] not in skip}


def _manifests(repo: Path) -> list:
    return sorted((repo / "state" / "warp").rglob("manifest.json"))


def _halt(repo: Path, run_id: str) -> None:
    halted_run.write_halt_marker(record_dir(repo, run_id), halted="criterion", sizing_rel=REL, run_base_sha=_BASE)


def _without_source(reply: dict) -> dict:
    return {k: v for k, v in reply.items() if k != PARK_SOURCE_REPLY_KEY}


def test_stop_after_plan_survives_chain_resume_launcher_child_and_stage(repo):
    before = _product_files(repo)

    ask = _emit_ask(repo)
    ask_id = ask["run_id"]
    assert OP_ASK_STAGE not in _script(ask)
    assert json.loads((record_dir(repo, ask_id) / STOP_AFTER_FILE).read_text(encoding="utf-8"))[STOP_AFTER_KEY] == STOP_AFTER_PLAN

    params = _chain_params_from(_script(ask), repo, ask_id)
    assert params["chain_parent"] == ask_id and params[STOP_AFTER_KEY] == STOP_AFTER_PLAN, params
    chained = _dispatch_emit({**params, "sizing_path": REL, "target_root": str(repo)})
    assert "error" not in chained, chained
    warp_id = chained["run_id"]
    assert warp_id.startswith("warp-") and warp_id != ask_id
    assert OP_ASK_STAGE not in _script(chained)
    warp_dir = record_dir(repo, warp_id)
    assert json.loads((warp_dir / STOP_AFTER_FILE).read_text(encoding="utf-8"))[STOP_AFTER_KEY] == STOP_AFTER_PLAN
    assert json.loads((warp_dir / "run.json").read_text(encoding="utf-8"))["chain_parent"] == ask_id

    # a chained emit that drops stop_after still parks: the lineage, not the params, carries it
    bare = _dispatch_emit({"ask": True, "sizing_path": REL, "target_root": str(repo), "chain_parent": warp_id})
    assert "error" not in bare, bare
    assert OP_ASK_STAGE not in _script(bare)
    assert (record_dir(repo, bare["run_id"]) / STOP_AFTER_FILE).is_file()

    _halt(repo, warp_id)
    record = hrb.build(repo, warp_id)
    assert record["stop_after"] == STOP_AFTER_PLAN
    resumed = (repo / record["composed_script_rel"]).read_text(encoding="utf-8")
    assert _PARKED_TEXT in resumed and OP_ASK_STAGE not in resumed

    child_params = fl.child_emit_params(repo, ask_id, {"ask": True, "sizing_path": REL, "target_root": str(repo)})
    assert child_params[STOP_AFTER_KEY] == STOP_AFTER_PLAN
    child = _dispatch_emit(child_params)
    assert "error" not in child, child
    assert OP_ASK_STAGE not in _script(child)
    assert (record_dir(repo, child["run_id"]) / STOP_AFTER_FILE).is_file()

    for run_id in (warp_id, bare["run_id"], child["run_id"]):
        reply = ask_stage._handler({"run_id": run_id, "plan_path": STAGEABLE_REL, "writes": []}, repo_root=repo)
        assert _without_source(reply) == {PARKED_REPLY_KEY: STOP_AFTER_PLAN, "plan_path": STAGEABLE_REL}, (run_id, reply)
        assert reply[PARK_SOURCE_REPLY_KEY]["inherited"] in (True, False), reply

    assert _manifests(repo) == []
    assert _product_files(repo) == before


def test_a_successor_that_cannot_park_refuses(repo):
    ask_id = _emit_ask(repo)["run_id"]

    with pytest.raises(ValueError, match="stop_after"):
        _refuse_non_ask_chain_parent({"chain_parent": ask_id, "target_root": str(repo)})

    with pytest.raises(ValueError, match="stop_after"):
        _dispatch_emit({"plan_path": STAGEABLE_REL, "chain_parent": ask_id, "target_root": str(repo)})
    assert not list((repo / RUN_DIR_ROOT).glob("warp-*.workflow.mjs"))


def test_a_run_with_no_marked_lineage_still_stages(repo):
    plain = _dispatch_emit({"ask": True, "sizing_path": REL, "target_root": str(repo)})
    assert "error" not in plain, plain
    assert OP_ASK_STAGE in _script(plain)
    assert not (record_dir(repo, plain["run_id"]) / STOP_AFTER_FILE).exists()


def test_resuming_a_parked_chained_run_does_not_park_again_but_a_fresh_successor_still_does(repo):
    ask = _emit_ask(repo)
    ask_id = ask["run_id"]
    params = _chain_params_from(_script(ask), repo, ask_id)
    warp_id = _dispatch_emit({**params, "sizing_path": REL, "target_root": str(repo)})["run_id"]
    stage_params = {"plan_path": STAGEABLE_REL, "writes": []}

    first = ask_stage._handler({"run_id": warp_id, **stage_params}, repo_root=repo)
    assert _without_source(first) == {PARKED_REPLY_KEY: STOP_AFTER_PLAN, "plan_path": STAGEABLE_REL}

    fresh = _dispatch_emit({"ask": True, "sizing_path": REL, "target_root": str(repo), "chain_parent": warp_id})["run_id"]
    assert fresh != warp_id
    halted_run.write_halt_marker(record_dir(repo, warp_id), halted=HALT_PARKED, sizing_rel=REL, plan_rel=STAGEABLE_REL)

    resumed = ask_stage._handler({"run_id": warp_id, **stage_params}, repo_root=repo)
    assert PARKED_REPLY_KEY not in resumed, resumed

    again = ask_stage._handler({"run_id": fresh, **stage_params}, repo_root=repo)
    assert _without_source(again) == {PARKED_REPLY_KEY: STOP_AFTER_PLAN, "plan_path": STAGEABLE_REL}
