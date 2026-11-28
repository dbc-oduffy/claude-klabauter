"""An empty verify-leg verdict is re-asked once in-run, then halts as verify-leg-empty and is never recorded not_met."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import (
    VERIFY_EMPTY_RETRY_JS,
    VERIFY_ITEM_MISSING,
    VERIFY_ITEMS_SCHEMA_JS,
    VERIFY_LEG_EMPTY,
    VERIFY_OWED_IDS_JS,
    compose_ask_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_SIZING = "state/sizings/x.yaml"


def _script(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=_SIZING,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def _halt_region(script: str) -> str:
    """The composed text from the owed-id derivation through the ledger-check halt clause."""
    start = script.index("const _rulable = ")
    end = script.index("}; }", script.index("{ _halted = { halted: 'ledger-check'", start)) + len("}; }")
    return script[start:end]


def _drive(
    monkeypatch: pytest.MonkeyPatch, verdicts: list, ledger_ids: list, withheld_ids: list | None = None,
    gate: dict | None = None,
) -> dict:
    """Runs the composed retry and halt JS under node with a stub verify leg answering `verdicts` in order."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    region = _halt_region(_script(monkeypatch))
    source = (
        "let _halted = null; let _ledger = null; let _verify = null; let _calls = 0; const _logs = [];\n"
        f"const log = (m) => _logs.push(m); const _runId = 'r1'; const _sizingRel = {_SIZING!r}; const _planRel = 'docs/plans/p.md';\n"
        f"const _manifest = {json.dumps({'ledger_item_ids': ledger_ids, 'withheld_ids': withheld_ids if withheld_ids is not None else []})};\n"
        f"const _gate = {json.dumps(gate or {})}; const _verifyLateIds = []; const _lateIds = []; const _recoveryWithheld = [];\n"
        "const _blockedChunks = []; const _incompleteChunks = []; const _lateUnplanned = []; const _ledgerSc = null;\n"
        "const _ledgerBlocking = (l) => (l?.items ?? []).filter((i) => i && i.status === 'not_met');\n"
        f"const _verdicts = {json.dumps(verdicts)};\n"
        "let _vMissing = ''; const _asks = [];\n"
        "const _runVerify = async () => { _calls += 1; _asks.push(_vMissing); _verify = _verdicts[Math.min(_calls - 1, _verdicts.length - 1)]; return _verify; };\n"
        + VERIFY_OWED_IDS_JS
        + "\n(async () => {\n"
        "_verify = await _runVerify();\n"
        + VERIFY_EMPTY_RETRY_JS
        + "\n_ledger = (_verify && Array.isArray(_verify.items)) ? { all_of_pm_ask: false, items: _verify.items, conflicts: [] } : null;\n"
        + region
        + "\nconsole.log(JSON.stringify({ halted: _halted, calls: _calls, ledger: _ledger, asks: _asks }));\n"
        "})();\n"
    )
    proc = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags()
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_two_empty_verdicts_retry_once_then_halt_verify_leg_empty_never_not_met(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], ["R1"])
    assert out["calls"] == 2
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert halted[VERIFY_LEG_EMPTY.replace("-", "_")] is True
    assert [i["id"] for i in halted["items"]] == ["R1"]
    assert all(i["status"] == VERIFY_LEG_EMPTY for i in halted["items"])
    assert VERIFY_LEG_EMPTY in halted["next_action"]
    assert "not_met" not in json.dumps(halted)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_every_owed_id_carries_verify_leg_empty_when_the_leg_returns_nothing_for_several_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    owed = ["R1", "R2", "R3"]
    out = _drive(monkeypatch, [{"items": []}], owed)
    assert out["calls"] == 2
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert [i["id"] for i in halted["items"]] == owed
    assert [i["status"] for i in halted["items"]] == [VERIFY_LEG_EMPTY] * len(owed)
    assert "not_met" not in json.dumps(halted)
    assert all(f"{i}" in halted["next_action"] for i in owed)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_verdict_on_the_retry_proceeds_without_a_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}, {"items": [{"id": "R1", "status": "met"}]}], ["R1"])
    assert out["calls"] == 2
    assert out["halted"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_returned_verdict_is_never_re_asked_and_a_genuine_not_met_stays_not_met(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": [{"id": "R1", "status": "not_met", "remaining": ["x"]}]}], ["R1"])
    assert out["calls"] == 1
    assert [i["status"] for i in out["halted"]["items"]] == ["not_met"]
    assert "verify_leg_empty" not in out["halted"]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_an_all_ruled_out_empty_verdict_is_not_re_asked_and_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], [], withheld_ids=["R1"])
    assert out["calls"] == 1
    assert out["halted"] is None


_JUDGE_MET = {"status": "met"}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_empty_items_with_judge_met_for_one_owed_row_re_asks_naming_it_and_halts_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": [], "judge": _JUDGE_MET}], ["R1"])
    assert out["calls"] == 2
    assert out["asks"][0] == ""
    assert "R1" in out["asks"][1]
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert halted["missing_item_ids"] == ["R1"]
    assert [i["id"] for i in halted["items"]] == ["R1"]
    assert "schema violation" in halted["items"][0]["evidence"]
    assert "R1" in halted["next_action"] and "schema violation" in halted["next_action"]
    assert "not_met" not in json.dumps(halted)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_partial_reply_missing_one_owed_row_re_asks_for_it_and_halts_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = {"items": [{"id": "R1", "status": "met"}], "judge": _JUDGE_MET}
    out = _drive(monkeypatch, [partial], ["R1", "R2"])
    assert out["calls"] == 2
    assert "R2" in out["asks"][1] and "R1" not in out["asks"][1]
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert halted["missing_item_ids"] == ["R2"]
    assert [(i["id"], i["status"]) for i in halted["items"]] == [("R2", VERIFY_ITEM_MISSING)]
    assert "R2" in halted["next_action"]
    assert "Firing the same sizing again" in halted["next_action"]
    assert "verify_leg_empty" not in halted


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_reply_carrying_an_item_per_owed_row_is_never_re_asked_and_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    full = {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "met"}], "judge": _JUDGE_MET}
    out = _drive(monkeypatch, [full], ["R1", "R2"])
    assert out["calls"] == 1
    assert out["halted"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_partial_reply_completed_by_the_re_ask_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    partial = {"items": [{"id": "R1", "status": "met"}]}
    full = {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "met"}]}
    out = _drive(monkeypatch, [partial, full], ["R1", "R2"])
    assert out["calls"] == 2
    assert out["halted"] is None


def _items_schema(owed: list) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    source = (
        f"const _manifest = {json.dumps({'ledger_item_ids': owed})}; const _gate = {{}}; const _verifyLateIds = ['L1'];\n"
        + VERIFY_OWED_IDS_JS + "\n" + VERIFY_ITEMS_SCHEMA_JS
        + "\nconsole.log(JSON.stringify(_verifyItemsSchema({ type: 'array', items: {} })));\n"
    )
    proc = subprocess.run([node, "-"], input=source, capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_the_items_schema_requires_an_item_per_owed_id_including_late_ids() -> None:
    schema = _items_schema(["R1"])
    assert schema["minItems"] == 2
    assert [c["contains"]["properties"]["id"]["const"] for c in schema["allOf"]] == ["R1", "L1"]


def test_the_verify_call_builds_its_items_schema_from_the_owed_list_and_names_the_owed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch)
    assert "_verifyItemsSchema({" in script
    assert VERIFY_ITEMS_SCHEMA_JS in script and VERIFY_OWED_IDS_JS in script
    assert "OWED ROWS: items holds one entry for EACH" in script
    assert script.index(VERIFY_ITEMS_SCHEMA_JS) < script.index("const _vAsk = ")


def test_the_retry_is_one_bounded_re_ask_with_no_loop() -> None:
    assert VERIFY_EMPTY_RETRY_JS.count("_runVerify()") == 1
    assert "while" not in VERIFY_EMPTY_RETRY_JS and "for (" not in VERIFY_EMPTY_RETRY_JS


def test_the_retry_sits_after_the_verify_join_and_before_the_overlay_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch)
    at = script.index(VERIFY_EMPTY_RETRY_JS)
    assert script.rindex("_verify = await _runVerify(); }", 0, at) < at
    assert at < script.index("_ledger = (_verify && Array.isArray(_verify.items))")


def _landed(**rows: str) -> dict:
    return {"resume_ledger": {"landed_met_by": rows}}


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_two_empty_verdicts_for_rows_landed_at_a_commit_credit_them_from_it_and_never_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], ["R1", "R2"], gate=_landed(R1="44cbe456d8", R2="44cbe456d8"))
    assert out["calls"] == 2
    assert out["halted"] is None
    assert [(i["id"], i["status"], i["met_by"]) for i in out["ledger"]["items"]] == [
        ("R1", "met", ["44cbe456d8"]), ("R2", "met", ["44cbe456d8"]),
    ]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_only_rows_with_a_landed_commit_are_credited_the_rest_still_halt(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], ["R1", "R2"], gate=_landed(R1="44cbe456d8"))
    assert [i["id"] for i in out["ledger"]["items"]] == ["R1"]
    halted = out["halted"]
    assert halted["missing_item_ids"] == ["R2"]
    assert "R1" not in [i["id"] for i in halted["items"]]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_verify_leg_empty_halt_never_advises_a_refire_of_the_same_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": []}], ["R1"], gate=_landed(R2="44cbe456d8"))
    halted = out["halted"]
    assert halted[VERIFY_LEG_EMPTY.replace("-", "_")] is True
    advice = halted["next_action"].lower()
    assert "re-fire" not in advice and "refire" not in advice
    assert "halted_run" in advice or "sha:" in advice


_EXAMPLE_GAME_REPO_C3F911_ROWS =[f"R{n}" for n in range(1, 15)]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_example_game_repo_c3f911_empty_verdict_on_r1_to_r14_is_retried_once_with_the_same_rows_then_halts_naming_them(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _drive(monkeypatch, [{"items": [], "judge": _JUDGE_MET}], _EXAMPLE_GAME_REPO_C3F911_ROWS)
    assert out["calls"] == 2
    assert all(row in out["asks"][1] for row in _EXAMPLE_GAME_REPO_C3F911_ROWS)
    halted = out["halted"]
    assert halted["halted"] == "ledger-check"
    assert halted["missing_item_ids"] == _EXAMPLE_GAME_REPO_C3F911_ROWS
    assert [i["id"] for i in halted["items"]] == _EXAMPLE_GAME_REPO_C3F911_ROWS
    assert "not_met" not in json.dumps(halted)
    assert out["ledger"]["items"] == []


def test_the_remnant_reverify_schema_requires_an_item_per_owed_row_with_the_helpers_in_its_own_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch)
    start = script.index("const _remnantComplete = async () => {")
    end = script.index("const _rulable = ", start)
    remnant = script[start:end]
    assert VERIFY_OWED_IDS_JS in remnant and VERIFY_ITEMS_SCHEMA_JS in remnant
    assert remnant.count("_verifyItemsSchema({") == 1
    assert remnant.index(VERIFY_ITEMS_SCHEMA_JS) < remnant.index("_verifyItemsSchema({")
