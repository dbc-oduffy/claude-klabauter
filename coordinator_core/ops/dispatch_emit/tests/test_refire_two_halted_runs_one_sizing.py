"""Two halted runs on one sizing: a refire resumes the caller's own newest run, or refuses naming every candidate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT, record_dir

pytestmark = pytest.mark.complete

REL = "state/sizings/2026-11-12-two-halts-one-sizing.yaml"
STEM = "2026-11-12-two-halts-one-sizing"
CALLER = "2dee147c-0000-0000-0000-000000000000"
FOREIGN = "c40a72a5-0000-0000-0000-000000000000"
OTHER = "99999999-0000-0000-0000-000000000000"
OLDER_WARP = f"warp-{STEM}-20261112T070648-cc36f8"
NEWER_ASK = "ask-20261112T121655-67ede5"
NOTE = "ruling posted while the run was live"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", CALLER)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-two-halts-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-12", "mode": "pm"}},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


def _halt(root: Path, run_id: str, owner: str, notes: bool = False) -> str:
    """A halted run of REL whose emission receipt names `owner`; `notes` posts an unstamped revise note."""
    rl.start(root, run_id)
    rl.bind(root, run_id, REL, ask_text="the ask")
    hr.write_halt_marker(record_dir(root, run_id), halted="criterion", sizing_rel=REL)
    script = root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("// s\n", encoding="utf-8")
    (script.parent / f"{script.name}.emitted.json").write_text(json.dumps({"session_id": owner}), encoding="utf-8")
    if notes:
        (root / RECORD_DIR_ROOT / run_id / rl.NOTES_FILE).write_text(
            json.dumps({"note": NOTE, "at": "2026-11-12T13:00:00Z"}) + "\n", encoding="utf-8"
        )
    return run_id


def test_the_callers_newer_run_wins_over_an_older_foreign_one_at_default_arguments(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, CALLER, notes=True)
    assert hr.halted_run_to_resume(repo, REL, caller_session=CALLER) == NEWER_ASK
    assert hr.halted_run_to_resume(repo, REL, build=False, caller_session=CALLER) == NEWER_ASK


def test_the_callers_older_run_wins_over_a_newer_foreign_one(repo):
    _halt(repo, OLDER_WARP, CALLER)
    _halt(repo, NEWER_ASK, FOREIGN)
    assert hr.halted_run_to_resume(repo, REL, caller_session=CALLER) == OLDER_WARP


def test_two_halted_asks_resume_the_callers_newest(repo):
    older = _halt(repo, "ask-20261112T070648-cc36f8", FOREIGN)
    _halt(repo, "ask-20261112T100000-aaaaaa", CALLER)
    newest_mine = _halt(repo, NEWER_ASK, CALLER)
    assert older != newest_mine
    assert hr.halted_run_to_resume(repo, REL, caller_session=CALLER) == newest_mine


def test_no_candidate_owned_by_the_caller_and_distinct_owners_refuses_naming_every_run(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, OTHER)
    with pytest.raises(hr.AmbiguousHaltedRuns) as raised:
        hr.halted_run_to_resume(repo, REL, caller_session=CALLER)
    assert set(raised.value.candidates) == {OLDER_WARP, NEWER_ASK}
    assert OLDER_WARP in str(raised.value) and NEWER_ASK in str(raised.value)


def test_ambiguity_refusal_reaches_the_caller_as_invalid_params_naming_every_run(repo):
    from coordinator_core import ipc

    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, OTHER)
    with pytest.raises(hr.AmbiguousHaltedRuns) as raised:
        hr.halted_run_to_resume(repo, REL, caller_session=CALLER)
    error = ipc._handler_exception_error(raised.value)
    assert error["code"] == ipc.INVALID_PARAMS
    assert OLDER_WARP in error["message"] and NEWER_ASK in error["message"]


def test_a_revised_stamp_breaks_a_tie_the_caller_does_not_own(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, OTHER)
    assert hr.mark_revised(record_dir(repo, OLDER_WARP), "2026-11-12T13:00:00Z")
    assert hr.halted_run_to_resume(repo, REL, caller_session=CALLER) == OLDER_WARP


def test_without_a_caller_session_the_newest_run_is_chosen_never_the_older(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, OTHER)
    assert hr.halted_run_to_resume(repo, REL) == NEWER_ASK


def test_a_single_halted_run_resolves_unchanged(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    assert hr.halted_run_to_resume(repo, REL, caller_session=CALLER) == OLDER_WARP
    assert hr.preferred_halted_run(repo, REL, caller_session=CALLER) is None


def test_a_default_dispatch_emit_resumes_the_callers_newer_run_with_its_notes(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, CALLER, notes=True)
    reply = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=repo)
    assert reply["run_id"] != OLDER_WARP
    assert hr._read(record_dir(repo, OLDER_WARP))["status"] == hr.STATUS_HALTED
    assert hr._read(record_dir(repo, NEWER_ASK))["status"] == hr.STATUS_RESUMED
    assert [e["note"] for e in rl.carried_notes(repo, reply["run_id"])] == [NOTE]


def test_a_default_dispatch_emit_refuses_and_leaves_both_runs_halted(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, OTHER)
    with pytest.raises(hr.AmbiguousHaltedRuns, match="ask-20261112T121655-67ede5"):
        op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=repo)
    for run_id in (OLDER_WARP, NEWER_ASK):
        assert hr._read(record_dir(repo, run_id))["status"] == hr.STATUS_HALTED


def test_the_submit_hook_route_refuses_naming_every_candidate(repo):
    _halt(repo, OLDER_WARP, FOREIGN)
    _halt(repo, NEWER_ASK, OTHER)
    envelope, result = hook._resume_route(repo, REL, {"tool_input": {}}, {}, CALLER, None)
    assert result is None
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert OLDER_WARP in hso["permissionDecisionReason"] and NEWER_ASK in hso["permissionDecisionReason"]
