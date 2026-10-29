"""A register row whose source_text opens with EM_CONTEXT_MARKER never blocks the composed ledger-check."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _em_context_row_ids, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.design_conformance import ANCHOR_PREFIX
from coordinator_core.ops.sizing_intake_contract import EM_CONTEXT_MARKER

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_SIZING = {
    "requirement_register": {
        "rows": [
            {"id": "R1", "source_text": "fix the bug"},
            {"id": "R2", "source_text": f"  {EM_CONTEXT_MARKER}\nupdate TODOs"},
            {"id": "R3", "source_text": f"{EM_CONTEXT_MARKER} x", "anchor": ANCHOR_PREFIX + "x"},
        ]
    }
}


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    monkeypatch.setattr(ask_compose, "load_sizing_effective", lambda *_: _SIZING)
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1",
        session_id=None, wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="x.mjs", **REVIEW_KW,
    )


def test_only_em_context_rows_are_derived(monkeypatch):
    monkeypatch.setattr(ask_compose, "load_sizing_effective", lambda *_: _SIZING)
    assert _em_context_row_ids("REPO", "x.yaml") == ["R2"]
    assert _em_context_row_ids("REPO", None) == []


def test_composed_gate_exempts_em_context_ids_before_any_status_check(monkeypatch):
    script = _script(monkeypatch)
    assert 'const _emContextIds = ["R2"];' in script
    body = script.split("const _ledgerBlocking = ", 1)[1]
    assert body.index("_emContextIds.includes(i.id) ||") < body.index("status === 'drifted'")
    assert "_emContextIds.includes(i.id)" in body.split("});", 1)[0]
