"""The ledger-check halt offers workflow.revise only the ids it accepts and names every other id apart."""

from __future__ import annotations

import jsonschema

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _MANIFEST_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, StageManifest
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


def test_the_rulable_set_reads_the_manifest_then_the_resume_verdict(monkeypatch):
    script = _script(monkeypatch)
    assert "_manifest?.rulable_ids ?? _gate.resume_ledger?.rulable_ids ?? []" in script


def test_revise_instruction_lists_only_blocking_ids_in_the_rulable_set(monkeypatch):
    script = _script(monkeypatch)
    assert "const _blkRulable = _blkIds.filter((id) => _rulable.includes(id));" in script
    text = _halt(script)
    assert "_blkRulable.length ?" in text
    assert "out_of_scope: [' + _blkRulable.join(', ') + ']" in text
    assert "out_of_scope: [row ids]" not in text


def test_non_register_ids_are_named_in_their_own_clause(monkeypatch):
    script = _script(monkeypatch)
    assert "const _blkForeign = _blkIds.filter((id) => !_rulable.includes(id));" in script
    text = _halt(script)
    assert "_blkForeign.length ?" in text
    assert "are not in the stage manifest rulable_ids: re-run the ledger check." in text
    assert text.index("_blkRulable.length ?") < text.index("_blkForeign.length ?")


def test_halted_carries_the_plan_and_next_action_the_refire(monkeypatch):
    text = _halt(_script(monkeypatch))
    assert f"{HALT_PLAN_FIELD}: _planRel" in text
    assert "Re-fire the same sizing: it executes the remaining work of the rows it keeps" in text


def test_a_manifest_with_rulable_ids_validates_against_the_manifest_schema():
    manifest = StageManifest(
        run_dir="d", plan_id=None, rows=[], review_declared_paths=[], marker_path="m", rulable_ids=("R1", "R2")
    ).to_json()
    jsonschema.validate(manifest, _MANIFEST_SCHEMA)
    assert manifest["rulable_ids"] == ["R1", "R2"]
