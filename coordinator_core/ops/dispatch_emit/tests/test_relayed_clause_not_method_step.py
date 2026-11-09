"""A relayed lettered clause whose text merely mentions "Method" earlier is not a method step; a
real method lead-in keeps its trace refusal (run ask-20261108T220805-38c2c8 R2/R3/R4)."""

from __future__ import annotations

from coordinator_core.ops import method_step
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

R2 = (
    "EM context (not the PM's words):\n"
    "A headless codex 0.167 session ran coordinator-invoke.py engine_ask.submit against klabauter "
    "19285ee532 and got JSON-RPC -32601 \"Method not found: 'engine_ask.submit'\". Make "
    "engine_ask.submit, workflow.fire_status and the ceremony ops reachable through the external "
    "coordinator-invoke route. Add a per-stage family: dispatch field, default-off.\n"
    "PM turn verbatim: 'fix it all.  Unimatrix bug should be fixed.' The PM P0, relayed by the "
    "G-EM: 'claude-klabauter and DoE build the infra to support Grok, Agy, and Codex integration for us, "
    "such that (a) they can if in their own EM session fire workflows just like we do natively, "
    "(b) we can use their agents as part of our workflows should we so choose'."
)
R3 = "(a) they can if in their own EM session fire workflows just like we do natively"
R4 = "(b) we can use their agents as part of our workflows should we so choose'."
STEP = "(a) capture the scan from the device"


def test_38c2c8_r2_lettered_parts_mint_unmarked_relayed_rows():
    entries = rl._lettered_entries(R2)
    assert [t[:3] for t, _ in entries] == ["(a)", "(b)"]
    assert all(anchor == "relayed" for _, anchor in entries)


def test_38c2c8_r3_r4_met_rows_pass_the_verdict_without_a_trace(tmp_path):
    items = tuple(
        rl.Item(rid, text, "relayed", "", anchor, "")
        for rid, (text, anchor) in zip(("R3", "R4"), rl._lettered_entries(R2))
    )
    ledger = rl.Ledger(items, False, "state/sizings/x.yaml", ())
    verdict = {"items": [{"id": "R3", "status": "met"}, {"id": "R4", "status": "met"}]}
    out = rl.judge(ledger, verdict, repo_root=tmp_path, plan_text="")
    assert out["pass"], out


def test_method_lead_sentence_still_marks_and_is_refused_without_a_trace(tmp_path):
    entries = rl._lettered_entries("Earlier remark. The method has two steps: " + STEP + " (b) bake")
    anchor = entries[0][1]
    assert method_step.is_method_step({"anchor": anchor})
    ledger = rl.Ledger((rl.Item("R1", STEP, "relayed", "", anchor, ""),), False, "x", ())
    out = rl.judge(ledger, {"items": [{"id": "R1", "status": "met"}]}, repo_root=tmp_path, plan_text="")
    assert not out["pass"]
    assert any("no mechanism trace" in f["reason"] for f in out["failures"])


def test_method_lead_on_its_own_line_still_marks():
    entries = rl._lettered_entries("Method steps:\n" + STEP + "\n(b) bake")
    assert entries and method_step.is_method_step({"anchor": entries[0][1]})


def test_method_word_in_an_earlier_sentence_does_not_mark():
    entries = rl._lettered_entries("The old method failed. Please fix: (a) the logo (b) the footer")
    assert entries and all(anchor == "relayed" for _, anchor in entries)
