"""A refire of a halted canvas-build sizing resumes that run: no scout sizing, plan or board, and the recorded approval carries.

Fixture: a month-subdir build sizing (`state/sizings/2026-11/`) with a canvas design_route approved through
`design_approval.record`, a `warp-<stem>-<stamp>` run halted `dependents_unstarted`, and a `workflow.revise` note.
Drives the default refire `{sizing}` path (`op._dispatch_emit`, `halted_run_to_resume`, `design_route.stage_refusal`).

The `flat-bound` param is the halt marker naming the pre-month-subdir sizing path. At HEAD it fails the AC1 resolver
assertions (`halted_run_to_resume` answers None; the emit mints a fresh `warp-` run): a resolver miss on the
month-subdir sizing path. The `month-bound` param and the AC2/AC3 assertions pass at HEAD, so the M+ chain
(`_m_plus_chain_js`) and an approval keyed to another board are not the mechanism on this path.
"""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, design_approval, design_route, op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, halted_run_to_resume, write_halt_marker

STEM = "2026-11-05-methodology-deck-match-mobile-canvas-build-dd0a4b"
REL = f"state/sizings/2026-11/{STEM}.yaml"
FLAT = f"state/sizings/{STEM}.yaml"
HALTED = f"warp-{STEM}-20261105T220859-1ccfd2"
BOARD_URL = "https://claude.ai/artifact/fixture-canvas-build"
NOTE = "three visual defects: header clipped, tab bar overlaps, icon colour"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
WATCHED = ("state/sizings", "docs/plans", "docs/design")


def _files(repo):
    return sorted(p.relative_to(repo).as_posix() for d in WATCHED if (repo / d).is_dir() for p in (repo / d).rglob("*") if p.is_file())


@pytest.fixture(params=["month-bound", "flat-bound"])
def repo(request, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    doc = {
        "schema": "sizing-object", "name": "methodology-deck-match-mobile-canvas", "intent": "build the mobile canvas",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [], "fork": None,
        "xl_exit": None, "status": "sized",
        "design_route": {"route": "canvas", "authority": "PM", "board_url": None, "approval": None},
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the mobile canvas"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-canvas-build-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-05", "mode": "pm"}},
    }
    path = tmp_path / REL
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    harvest = tmp_path / "docs/design/methodology-deck-match-mobile-canvas/harvest/harvest.json"
    harvest.parent.mkdir(parents=True)
    harvest.write_text("{}", encoding="utf-8")
    assert design_approval.record(tmp_path, REL, board_url=BOARD_URL) == {"exit_code": 0}
    rl.start(tmp_path, HALTED)
    bound = REL if request.param == "month-bound" else FLAT
    rl.bind(tmp_path, HALTED, REL, ask_text="the ask")
    write_halt_marker(record_dir(tmp_path, HALTED), halted="dependents_unstarted", sizing_rel=bound, reason="C2 held on C1")
    rl.revise(tmp_path, HALTED, note=NOTE)
    return tmp_path


def _fire(repo, **extra):
    return op._dispatch_emit({"ask": True, "sizing_path": REL, **extra}, repo_root=repo)


def _payload(repo):
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    return design_route.read(doc, REL)[0]


def test_ac1_the_resolver_returns_the_halted_warp_run_for_the_month_subdir_sizing(repo):
    assert halted_run_to_resume(repo, REL) == HALTED
    assert halted_run_to_resume(repo, REL, build=False) == HALTED


def test_ac1_the_submit_hook_resolves_the_halted_run_for_the_default_refire(repo, monkeypatch):
    from coordinator_core.hooks import submit_engine_ask as hook

    launched = []

    def launch(root, parent_id, children, session_id):
        launched.extend(children)
        return repo / "launcher.mjs"

    monkeypatch.setattr(hook, "_spawn_recompose", lambda *_a: False)
    payload = {"tool_input": {"name": hook.SUBMIT_WORKFLOW_NAME, "args": {"sizing": REL}}}
    envelope, _ = hook._resume_route(repo, REL, payload, {"ask": True, "sizing_path": REL}, None, launch)
    assert envelope is not None and [c.run_id for c in launched] == [HALTED]


def test_ac1_the_default_refire_resumes_the_halted_run_carrying_the_revise_note(repo):
    reply = _fire(repo)
    assert reply["run_id"] == HALTED
    marker = json.loads((record_dir(repo, HALTED) / HALTED_FILE).read_text(encoding="utf-8"))
    assert marker["status"] == "resumed"
    assert [e["note"] for e in rl.carried_notes(repo, reply["run_id"])] == [NOTE]


def test_ac2_the_refire_writes_no_new_sizing_plan_or_board(repo):
    before = _files(repo)
    _fire(repo)
    assert _files(repo) == before


def test_ac3_the_recorded_approval_carries_through_the_refire(repo):
    assert design_route.stage_refusal(_payload(repo)) is None
    _fire(repo)
    payload = _payload(repo)
    assert payload["approved"] is True and payload["board"]["url"] == BOARD_URL
    assert design_route.stage_refusal(payload) is None


def test_an_unapproved_build_board_still_refuses(repo):
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["design_route"]["approval"] = None
    doc["design_route"]["board_url"] = None
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    assert design_route.stage_refusal(_payload(repo)) is not None
