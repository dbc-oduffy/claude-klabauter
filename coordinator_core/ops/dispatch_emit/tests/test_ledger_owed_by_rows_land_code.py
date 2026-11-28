"""A not_met row owed only to the manifest's EM/operator rows lands the code instead of halting ledger-check.

The verifier often leaves `owed_by` off a row whose remaining legs all name an owed row ("C3: measure head
vs baseline"); the engine derives it, `_ledgerBlocking` lets an owed row through, and `judge` lists it open.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import OWED_BY_DERIVE_JS, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import OwedRow
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process
_NODE = shutil.which("node")
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_OWED = [
    {"id": "C0", "reason": "em-performed", "detail": "EM: perform it"},
    {"id": "C3", "reason": "em-performed", "detail": "EM: perform it"},
]


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        **REVIEW_KW,
    )


def _node(program: str):
    proc = subprocess.run(
        [_NODE, "-"], input=program, capture_output=True, text=True, timeout=60, check=True,
        **no_console_creationflags(),
    )
    return json.loads(proc.stdout)


def _derive(items: list[dict], owed=_OWED, gated=(), slot_withheld=()) -> list[dict]:
    program = (
        f"const _manifest = {{ owed: {json.dumps(owed)}, gated: {json.dumps(list(gated))}, "
        f"slot_withheld: {json.dumps(list(slot_withheld))} }}; const _ledger = {{ items: {json.dumps(items)} }};\n"
        + OWED_BY_DERIVE_JS
        + "\nprocess.stdout.write(JSON.stringify(_ledger.items));"
    )
    return _node(program)


def _blocking(script: str, items: list[dict]) -> list[str]:
    start = script.index("const _postCommitActs")
    anchor = script.index("const _ledgerBlocking")
    end = re.compile(r"\n\s*\}\);").search(script, anchor).end()
    program = (
        f"const _manifest = {{ gated: [], owed: {json.dumps(_OWED)} }}; const _gate = {{}};\n"
        "const _recoveryWithheld = [];\n"
        + script[start:end]
        + f"\nprocess.stdout.write(JSON.stringify(_ledgerBlocking({json.dumps({'items': items})}).map((i) => i.id)));"
    )
    return _node(program)


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_remaining_that_names_only_owed_rows_gets_owed_by():
    item = {"id": "R1", "status": "not_met", "remaining": [
        "C0: EM pins 779dc2eb01 baseline on full and held-out banks", "C3: EM measures head vs baseline", "commit",
    ]}
    assert _derive([item])[0]["owed_by"] == ["C0", "C3"]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_an_owed_id_named_with_an_em_marker_beside_prose_counts():
    item = {"id": "R1", "status": "not_met", "remaining": ["Measured re-run after the fix (plan row C3, EM-performed)"]}
    assert _derive([item])[0]["owed_by"] == ["C3"]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_one_gap_that_is_not_owed_leaves_the_row_unowed():
    item = {"id": "R1", "status": "not_met", "remaining": ["C3: measure head", "add the missing test file"]}
    assert "owed_by" not in _derive([item])[0]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_bare_owed_id_prefix_without_an_em_marker_is_not_owed():
    item = {"id": "R1", "status": "not_met", "remaining": ["C3: handler missing"]}
    assert "owed_by" not in _derive([item])[0]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_prose_mention_of_an_owed_id_without_an_em_marker_is_not_owed():
    item = {"id": "R1", "status": "not_met", "remaining": ["the C3 helper still raises KeyError"]}
    assert "owed_by" not in _derive([item])[0]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_an_existing_owed_by_or_withheld_by_is_kept():
    items = [
        {"id": "R1", "status": "not_met", "remaining": ["C3: x"], "owed_by": ["C0"]},
        {"id": "R2", "status": "not_met", "remaining": ["C3: x"], "withheld_by": "G1"},
    ]
    out = _derive(items)
    assert out[0]["owed_by"] == ["C0"] and "owed_by" not in out[1]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_leg_naming_an_externally_blocked_em_run_row_gets_owed_by():
    gated = [{"id": "C4", "reason": "external_gate"}]
    item = {"id": "R1", "status": "not_met", "remaining": ["C4: em_run row externally blocked on the import verb", "commit"]}
    assert _derive([item], owed=[], gated=gated)[0]["owed_by"] == ["C4"]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_leg_naming_a_slot_owed_row_gets_owed_by():
    slot = [{"id": "C5", "title": "measure", "register_rows": ["R1"]}]
    item = {"id": "R1", "status": "not_met", "remaining": ["C5: needs_slot measurement waits on a box slot"]}
    assert _derive([item], owed=[], slot_withheld=slot)[0]["owed_by"] == ["C5"]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_gated_id_without_a_blocked_or_owed_marker_or_beside_other_gaps_is_not_owed():
    gated = [{"id": "C4", "reason": "external_gate"}]
    bare = {"id": "R1", "status": "not_met", "remaining": ["C4: handler missing"]}
    mixed = {"id": "R2", "status": "not_met", "remaining": ["C4: externally blocked", "add the missing test file"]}
    out = _derive([bare, mixed], owed=[], gated=gated)
    assert "owed_by" not in out[0] and "owed_by" not in out[1]


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_row_owed_to_a_gated_or_slot_row_does_not_block(monkeypatch):
    script = _script(monkeypatch)
    start = script.index("const _postCommitActs")
    anchor = script.index("const _ledgerBlocking")
    end = re.compile(r"\n\s*\}\);").search(script, anchor).end()
    items = [
        {"id": "R1", "status": "not_met", "remaining": ["C4: x"], "owed_by": ["C4"]},
        {"id": "R2", "status": "not_met", "remaining": ["C5: x"], "owed_by": ["C5"]},
        {"id": "R3", "status": "not_met", "remaining": ["C9: x"], "owed_by": ["C9"]},
    ]
    program = (
        "const _manifest = { gated: [{ id: 'C4' }], owed: [], slot_withheld: [{ id: 'C5' }] }; const _gate = {};\n"
        "const _recoveryWithheld = [];\n"
        + script[start:end]
        + f"\nprocess.stdout.write(JSON.stringify(_ledgerBlocking({json.dumps({'items': items})}).map((i) => i.id)));"
    )
    assert _node(program) == ["R3"]


def test_the_derivation_runs_inside_the_ledger_normaliser(monkeypatch):
    script = _script(monkeypatch)
    assert script.index(OWED_BY_DERIVE_JS) < script.index("_normaliseLedger();")


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_a_row_owed_to_manifest_rows_does_not_block(monkeypatch):
    script = _script(monkeypatch)
    items = [
        {"id": "R1", "status": "not_met", "remaining": ["C3: x"], "owed_by": ["C3"]},
        {"id": "R2", "status": "not_met", "remaining": ["C3: x"], "owed_by": ["C77"]},
        {"id": "R3", "status": "not_met", "remaining": ["real work"]},
    ]
    assert _blocking(script, items) == ["R2", "R3"]


def test_judge_lists_an_owed_by_row_open_and_passes():
    ledger = rl.Ledger((rl.Item("R1", "a", "pm", "t"),), False)
    verdict = {"items": [{"id": "R1", "status": "not_met", "remaining": ["C3: x"], "owed_by": ["C3"]}]}
    operator = rl.owed_rows((OwedRow(id="C3", reason="em-performed", detail="EM: perform it"),))
    result = rl.judge(ledger, verdict, operator=operator)
    assert result["pass"] is True
    assert [o["id"] for o in result["open"]] == ["R1"] and "C3" in result["open"][0]["reason"]


def test_judge_still_refuses_owed_by_naming_a_row_the_manifest_never_owed():
    ledger = rl.Ledger((rl.Item("R1", "a", "pm", "t"),), False)
    verdict = {"items": [{"id": "R1", "status": "not_met", "remaining": ["x"], "owed_by": ["C9"]}]}
    result = rl.judge(ledger, verdict, operator=rl.owed_rows((OwedRow(id="C3", reason="em-performed", detail="d"),)))
    assert result["pass"] is False
