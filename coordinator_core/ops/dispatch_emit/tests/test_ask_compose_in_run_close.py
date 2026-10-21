"""The run commits in-run only when closeable; otherwise it returns revise_and_refire, never a commit hand-off."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.warp_fire import fire_command

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_LIVE = "if (_closeable && _nextAction && _nextAction.kind === 'terminal_commit' && _RUN_TOKEN) {"


def _script(monkeypatch, **kw) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    args = dict(
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
    args.update(kw)
    return compose_ask_script(**args)


def test_closeable_predicate_gates_next_action_and_both_commit_stages(monkeypatch):
    script = _script(monkeypatch)
    assert script.count("const _closeable = ") == 1
    pred = script[script.index("const _closeable = ") :].split("\n", 1)[0]
    assert "!_halted" in pred and "_manifest.error" in pred
    assert "_deliveryVerdict.verdict === 'FAIL'" in pred
    assert "_closeable ? { kind: (" in script and "? 'none' : 'terminal_commit')" in script
    assert script.count(_LIVE) == 1
    assert ask_compose._DOCS_PRE_TAIL_GUARD in script
    assert script.index(ask_compose._DOCS_PRE_TAIL_GUARD) < script.index("const _closeable = ")
    assert script.index("const _closeable") < script.index("_nextAction = _closeable")


def test_criterion_not_met_blocks_close_when_a_judge_runs(monkeypatch):
    monkeypatch.setattr(ask_compose, "_criterion_judge_brief", lambda *a, **k: ("judge brief", {"type": "object"}))
    script = _script(monkeypatch)
    var = ask_compose._emit._FALSIFIER_RESULT_VAR
    pred = script[script.index("const _closeable = ") :].split("\n", 1)[0]
    assert "!_criterionOpen" in pred
    assert f"const _criterionOpen = !!({var} && {var}.status === 'not_met');" in script
    assert (
        f"_criterionOpen ? (String({var}.reason ?? '').startsWith('not wired up: ') ? String({var}.reason).slice(0, 200) : "
        f"'criterion not_met' + ({var}.reason ? ': ' + String({var}.reason).slice(0, 200) : ''))"
    ) in script


def test_revise_and_refire_rides_beside_next_action(monkeypatch):
    script = _script(monkeypatch)
    assert "let _reviseAndRefire = _closeable ? null : _mkRevise();" in script
    assert "revise_and_refire: _reviseAndRefire" in script
    assert ask_compose._lit(fire_command("state/sizings/x.yaml")) in script
    assert "workflow.revise" in script


def test_refire_without_a_sizing_renders_at_run_time(monkeypatch):
    script = _script(monkeypatch, sizing_rel=None, prompt="do it")
    assert "(_sizingRel ? " in script and "JSON.stringify(_sizingRel)" in script


def test_commit_prompt_names_the_agent_definition_clause(monkeypatch):
    script = _script(monkeypatch)
    assert ask_compose._lit("A run's commit stage")[1:-1] in script
    assert "no `paths` array is owed" in script


def test_ledger_halt_composes_no_terminal_commit_hand_off(monkeypatch):
    script = _script(monkeypatch)
    ledger_halt = script.index("halted: 'ledger-check'")
    assert ledger_halt < script.index("const _closeable")


_REVISE = "{ kind: 'revise_and_refire', op: 'workflow.revise', params: { run_id: _runId, sizing: _sizingRel, halted: _halted } }"


def test_in_run_stopped_next_action_is_revise_and_refire_never_a_commit(monkeypatch):
    script = _script(monkeypatch)
    assert f": {_REVISE});" in script
    assert "get terminal_commit_cli() { return this.next_action.kind === 'terminal_commit' " in script


def test_refused_commit_halts_and_returns_revise_and_refire(monkeypatch):
    script = _script(monkeypatch)
    tail = script[script.index("_commit = await") :]
    refused = tail[tail.index("else { if (!_halted) _halted = { halted: 'commit'") :].split("\n", 1)[0]
    assert "refused: _commit?.refused ?? null" in refused and "error: _commit?.error ?? null" in refused
    assert _REVISE in refused and "_reviseAndRefire = _mkRevise();" in refused
    assert "kind: 'close_job'" in tail[: tail.index("else { if (!_halted)")]
    assert "blob_drift: _commit?.blob_drift ?? null" in refused


def test_roadmap_commit_halt_carries_blob_drift():
    lines = ask_compose.roadmap_commit_lines(head="h", anchor="a", repo_root="r", run_id="run1")
    halt = next(ln for ln in lines if "halted: 'commit'" in ln)
    assert "blob_drift: _commit?.blob_drift ?? null" in halt


def test_commit_prompt_carries_a_path_command_and_an_uncopied_params_block(monkeypatch):
    script = _script(monkeypatch, repo_root="C:/repo")  # abs-path-ok: drive-letter-shaped fixture root
    command = (
        f"coordinator-invoke {OP_NAME} --params-file "
        "C:/repo/scratch/warp/run-1/terminal-commit-params.json"  # abs-path-ok: fixture root
    )
    # The product commit stage and the halt-record commit both run it.
    assert script.count(command) == 2
    start = script.rindex("phase: commit", 0, script.index("commit stage (`coordinator:git-commit-agent`"))
    prompt = script[start : script.index("Return from its JSON reply", start)]
    assert "--params-file -" not in prompt and "<<'JSON'" not in prompt
    assert "_terminalCommitCli(" not in prompt
    assert prompt.count("_terminalCommitParamsBlock({ ..._nextAction.params") == 1
    assert script.count("function _terminalCommitParamsBlock(") == 1
    assert "Do not copy, edit or pass it" in prompt
    assert prompt.index(command) < prompt.index("_terminalCommitParamsBlock({")


def test_token_less_composition_keeps_the_commit_hand_off(monkeypatch):
    script = _script(monkeypatch, in_run_commit=False)
    assert "revise_and_refire', op" not in script
    assert "{ kind: 'none', op: null, params: null });" in script
    assert "get terminal_commit_cli() { return _terminalCommitCli(this.next_action.params); }" in script


def test_landed_close_job_carries_released_sibling_fire_calls(monkeypatch):
    script = _script(monkeypatch)
    tail = script[script.index("_commit = await") :]
    landed = tail[tail.index("_nextAction = { kind: 'close_job'") :].split("\n", 1)[0]
    assert "fire: _commit.close?.siblings?.released ?? []" in landed


def test_return_fire_falls_back_to_commit_released_list(monkeypatch):
    script = _script(monkeypatch)
    assert (
        "fire: _splitExit ? _splitExit.fire : (_halted && _halted.halted === 'research' && "
        "Array.isArray(_halted.fire) ? _halted.fire : (_commit?.close?.siblings?.released ?? [])),"
    ) in script
