"""The composed ledger-check passes an empty verdict only when every register row is withheld, and never advises a refire with nothing to re-execute."""

from __future__ import annotations

import jsonschema

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _MANIFEST_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def _halt(script: str) -> str:
    start = script.index("halted: 'ledger-check'")
    return script[start : script.index("}; }", start)]


def test_the_bare_empty_items_halt_clause_is_gone(monkeypatch):
    assert "!(_ledger.items ?? []).length" not in _script(monkeypatch)


def test_ledger_ids_prefer_the_resume_verdict_over_the_manifest(monkeypatch):
    script = _script(monkeypatch)
    assert "_gate.resume_ledger?.ledger_item_ids ?? _manifest?.ledger_item_ids ?? null" in script
    assert "_gate.resume_ledger?.withheld_ids ?? _manifest?.withheld_ids ?? null" in script


def test_an_empty_verdict_passes_iff_no_items_are_owed_and_some_row_is_withheld(monkeypatch):
    script = _script(monkeypatch)
    assert (
        "const _allRuledOut = Array.isArray(_ledgerIds) && !_ledgerIds.length "
        "&& Array.isArray(_withheldIds) && _withheldIds.length > 0;"
    ) in script
    assert "const _emptyVerdict = !!_ledger && (_ledger.items ?? []).length === 0 && !_allRuledOut;" in script
    assert "!_ledger || _emptyVerdict || _ledgerBlocking(_ledger).length" in script


def test_an_owed_but_unjudged_empty_verdict_blocks_on_the_owed_ids(monkeypatch):
    script = _script(monkeypatch)
    assert "const _owedIds = _emptyVerdict && Array.isArray(_ledgerIds) ? _ledgerIds : [];" in script
    text = _halt(script)
    assert "_owedIds.map((id) => ({ id, status: 'not_met' }))" in text
    assert "were owed a verdict and the verify leg returned none" in text


def test_next_action_has_no_unreadable_label_and_gates_the_refire_on_blocking_ids(monkeypatch):
    text = _halt(_script(monkeypatch))
    assert "(ledger unreadable)" not in text
    assert "(_blkIds.length ? ' Re-fire the same sizing" in text


def test_a_null_ledger_names_the_verify_leg_cause(monkeypatch):
    text = _halt(_script(monkeypatch))
    assert "The verify leg returned no items list" in text


def test_manifest_schema_accepts_the_ledger_id_arrays():
    props = _MANIFEST_SCHEMA["properties"]
    assert props["ledger_item_ids"] == props["withheld_ids"] == {"type": "array", "items": {"type": "string"}}
    jsonschema.validate(
        {
            "run_dir": "d", "plan_id": None, "rows": [], "review_declared_paths": [], "marker_path": "m",
            "ledger_item_ids": [], "withheld_ids": ["R1"],
        },
        _MANIFEST_SCHEMA,
    )
