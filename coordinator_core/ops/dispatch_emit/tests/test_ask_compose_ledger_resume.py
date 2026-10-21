"""A resume_ledger gate re-judges only the blocking rows and reaches the normal ledger halt and commit."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _GATE_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.land_run_contract import RECONCILE_CLAUSE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_the_gate_schema_carries_resume_ledger():
    assert _GATE_SCHEMA["properties"]["resume_ledger"] == {"type": ["object", "null"]}


def test_resume_binds_the_plan_and_takes_the_xs_tail_before_any_plan_or_executor_call(monkeypatch):
    script = _script(monkeypatch)
    bind = script.index("const _resumeLedger = !_halted && !!_gate.resume_ledger;")
    assert (
        "_planRel = _gate.resume_ledger.plan; if (_ledgerUnlanded.length) { _ledgerExec = true; } "
        "else if (!_ledgerOwes) { _gate = { ..._gate, arm: 'xs' }; }"
    ) in script[bind:]
    assert bind < script.index("label: 'plan'")
    execute = script.index("if (!_halted && (!_resumeLedger || _ledgerExec)) {\n  phase('Execute');")
    assert bind < execute < script.index("label: 'verify'")


# Before the fix the block read `_planRel = _gate.resume_ledger.plan; _gate = { ..._gate, arm: 'xs' }; }`
# for every ledger resume, so an m_plus verdict owing work planned as xs and dispatched no row.
_BEFORE_STATE = "_planRel = _gate.resume_ledger.plan; _gate = { ..._gate, arm: 'xs' }; }"


# The substring tests below pin the composed JS shape only; dispatch of the added rows is proven by
# test_refire_replan_added_rows_dispatch_e2e.py.
def test_before_state_unconditional_xs_is_gone(monkeypatch):
    assert _BEFORE_STATE not in _script(monkeypatch)


def test_a_work_owing_m_plus_resume_keeps_its_arm_and_replans_with_the_owed_work(monkeypatch):
    script = _script(monkeypatch)
    owes = script.index("const _ledgerOwes = _resumeLedger && (_gate.arm === 'm_plus' || _gate.arm === 's')")
    assert ".remaining ?? []).length > 0" in script[owes : owes + 400]
    assert "else if (!_ledgerOwes) { _gate = { ..._gate, arm: 'xs' }; }" in script
    leg = script.index("label: 'ledger-replan'")
    prompt = script[leg - 4200 : leg]
    assert "_gate.resume_ledger.blocking.join(', ')" in prompt
    assert "_gate.resume_ledger.remaining ?? {}" in prompt
    assert "_gate.resume_ledger.revise_notes ?? []" in prompt
    assert "added_rows" in prompt
    assert "agentType: 'coordinator:plan-author'" in script[leg:][:200]
    assert "label: 'ledger-restage'" not in script
    assert script.index("const _ledgerOwes") < leg < script.index("label: 'verify'")


def test_the_re_plan_adds_rows_that_are_staged_by_id_and_dispatched(monkeypatch):
    script = _script(monkeypatch)
    leg = script.index("label: 'ledger-replan'")
    prompt = script[leg - 4200 : leg]
    assert "with only_rows set to the ids of exactly the rows you added" in prompt
    assert "' together with these unlanded rows: '" in prompt
    assert "if (_ledgerAdded.length) { _ledgerExec = true; await _adoptStageReply(" in script
    assert (
        "else { if (!_ledgerUnlanded.length) { _gate = { ..._gate, arm: 'xs' }; } "
        "_stagedManifest = _gateStaged; _relayGap = _gateGap; _diskHalt = _gateDisk; }"
    ) in script
    assert script.count("if (!_halted && (!_resumeLedger || _ledgerExec)) {") == 2
    assert "if (_resumeLedger && !_ledgerExec && ((_gate.resume_ledger.blocking ?? []).length || _reviseNoted)) {" in script


def test_a_re_judge_only_resume_keeps_the_xs_tail(monkeypatch):
    # _ledgerOwes is false unless the arm is m_plus or s AND a blocking id has non-empty remaining; with
    # no unlanded row either, the xs force and the remaining-work leg both stay (AC5).
    script = _script(monkeypatch)
    owes = script[script.index("const _ledgerOwes"):][:400]
    assert "_gate.arm === 'm_plus'" in owes and "_gate.arm === 's'" in owes and ".some(" in owes
    assert script.count("label: 'ledger-remaining'") == 1


def test_unlanded_rows_set_ledger_exec_without_forcing_xs(monkeypatch):
    script = _script(monkeypatch)
    assert (
        "const _ledgerUnlanded = _resumeLedger ? (_gate.resume_ledger.unlanded ?? []) : [];"
    ) in script
    assert "if (_ledgerUnlanded.length) { _ledgerExec = true; }" in script
    assert script.index("const _ledgerUnlanded") < script.index("const _ledgerOwes")


def test_resume_composes_no_review_or_test_call_before_verify(monkeypatch):
    script = _script(monkeypatch)
    review = script.index("if (!_halted && _gate.arm !== 'xs' && !_noReviewPaths) {")
    assert script.index("_gate = { ..._gate, arm: 'xs' }") < review < script.index("label: 'verify'")


def test_the_verify_prompt_names_exactly_the_blocking_ids(monkeypatch):
    script = _script(monkeypatch)
    assert "_gate.resume_ledger.blocking.join(', ')" in script
    assert "Return an item for each of them and no other item." in script
    assert script.index("_gate.resume_ledger.blocking.join") < script.index("label: 'verify'")


def test_the_clause_demands_work_changed_since_the_halt_and_lists_the_notes(monkeypatch):
    script = _script(monkeypatch)
    ledger = script.replace(ask_compose._lit(" " + RECONCILE_CLAUSE), "")
    assert ledger != script
    assert "already ran" not in ledger and "already landed" not in ledger
    assert "met only on work changed since the halt commit" in script
    assert "JSON.stringify(_gate.resume_ledger.revise_notes ?? [])" in script


def test_a_remaining_work_leg_runs_for_blocking_rows_before_the_verify(monkeypatch):
    script = _script(monkeypatch)
    guard = "if (_resumeLedger && !_ledgerExec && ((_gate.resume_ledger.blocking ?? []).length || _reviseNoted)) {"
    assert script.count("label: 'ledger-remaining'") == 1
    assert script.index(guard) < script.index("label: 'ledger-remaining'") < script.index("label: 'verify'")
    label_at = script.index("label: 'ledger-remaining'")
    leg = script[script.rindex("Ledger-resume remaining-work leg", 0, label_at) : label_at]
    assert "_gate.resume_ledger.remaining ?? {}" in leg
    assert "_gate.resume_ledger.revise_notes ?? []" in leg
    assert "notes.jsonl" in leg
    assert "flatMap((r) => r.writes ?? [])" in leg
    assert "agentType: 'coordinator:executor'" in script[script.index("label: 'ledger-remaining'"):][:200]


def test_a_revise_note_runs_the_remaining_leg_even_when_no_row_blocks(monkeypatch):
    # Blocking [] with a note used to skip the executor leg: the note was read by nothing that wrote.
    script = _script(monkeypatch)
    guard = "if (_resumeLedger && !_ledgerExec && ((_gate.resume_ledger.blocking ?? []).length || _reviseNoted)) {"
    assert script.count(guard) == 1
    assert "const _reviseNoted = _resumeLedger && (_gate.resume_ledger.revise_notes ?? []).length > 0;" in script
    assert script.index("const _reviseNoted") < script.index(guard) < script.index("label: 'ledger-remaining'")
    # An all-landed resume still skips the leg unless a note asks for work.
    assert "if (!_allLandedResume || _reviseNoted) { try {" in script


def test_the_terminal_commit_params_carry_the_resume_block(monkeypatch):
    script = _script(monkeypatch)
    assert (
        "_nextAction.params.resume_ledger = { run_id: _runId, halt_sha: _gate.resume_ledger.halt_sha ?? null, "
        "blocking: _gate.resume_ledger.blocking ?? [] }"
    ) in script


def test_the_ledger_halt_next_action_no_longer_asks_for_an_out_of_band_fix(monkeypatch):
    script = _script(monkeypatch)
    assert "met out of band" not in script
    assert "executes the remaining work of the rows it keeps" in script


def test_the_clause_names_em_discharged_rows_from_the_pinned_key(monkeypatch):
    script = _script(monkeypatch)
    assert "EM-discharged rows (performed by the EM, recorded via em-discharge): " in script
    assert "_gate.resume_ledger.em_discharged" in script
    assert script.count("em_discharged") == script.count("_gate.resume_ledger.em_discharged")


def test_the_discharge_clause_is_gated_on_a_non_empty_em_discharged_map():
    clause = ask_compose._RESUME_LEDGER_CLAUSE_JS
    gate = "Object.keys(_gate.resume_ledger.em_discharged ?? {}).length ? "
    assert gate in clause
    assert clause.index(gate) < clause.index("EM-discharged rows")
    assert clause.index("Object.entries(_gate.resume_ledger.em_discharged).map(([k, v]) => k + ': ' + v)") > clause.index(gate)
    assert clause.endswith(": '') : '')")


def test_the_ledger_halt_still_precedes_the_commit_on_the_resume_path(monkeypatch):
    script = _script(monkeypatch)
    halt = script.index("halted: 'ledger-check'")
    assert script.index("label: 'verify'") < halt < script.index("phase('commit')")


def test_the_gate_reply_prompts_name_resume_ledger(monkeypatch):
    assert "resume_rows, resume_ledger and spine_plan" in _script(monkeypatch)
