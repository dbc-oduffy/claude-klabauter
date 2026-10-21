"""The commit stage and the halt commit carry the docs stage's doc_paths to dispatch.land_run."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _COMMIT_SCHEMA, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD = "(typeof _docs !== 'undefined' && _docs && Array.isArray(_docs.doc_paths)) ? _docs.doc_paths : []"
_FN = "async function _commitHaltRecord()"


def _compose(monkeypatch, **over) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs",
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_commit_stage_and_halt_commit_carry_doc_paths(monkeypatch):
    script = _compose(monkeypatch, in_run_commit=True)
    stage = script[script.index("_terminalCommitParamsBlock({ ..._nextAction.params") :].split("\n", 1)[0]
    assert f"doc_paths: {_GUARD}" in stage
    halt = script[script.index(_FN) :].split("\n", 1)[0]
    assert f"doc_paths: {_GUARD}" in halt
    assert "run_artifacts" in stage and "run_artifact_residue" in halt


def test_script_without_an_in_run_commit_binds_no_doc_paths(monkeypatch):
    script = _compose(monkeypatch)
    assert _FN not in script
    assert "_terminalCommitParamsBlock({" not in script
    for line in script.splitlines():
        if "doc_paths: " in line and "_terminalCommitParamsBlock" in line:
            assert _GUARD in line


def test_commit_schema_names_the_run_artifact_keys():
    props = _COMMIT_SCHEMA["properties"]
    for key in ("run_artifacts", "run_artifact_residue"):
        assert props[key] == {"type": ["object", "null"]}
