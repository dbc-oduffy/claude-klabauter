"""The composed commit stage stamps the landed close block with the script's own close key."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch, script_path="scratch/warp/run-1.workflow.mjs") -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path=script_path,
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_own_close_key_precedes_commit_and_close_reads(monkeypatch):
    script = _script(monkeypatch)
    key = script.index("const _ownCloseKey = ")
    assert key < script.index("_commit = await")
    assert key < script.index("_commit.close")
    assert key < script.index("kind: 'close_job'")
    decl = script[key : script.index(";", key)]
    assert "_nextAction.params.script_path ?? " in decl
    assert "scratch/warp/run-1.workflow.mjs" in decl


def test_replacement_keeps_relayed_id_and_guards_empty_key(monkeypatch):
    script = _script(monkeypatch)
    fix = script[script.index("if (_ownCloseKey && _commit"):]
    fix = fix[: fix.index("_blockedRows")]
    assert "_commit.committed === true" in fix
    assert "_commit.close.run_id !== _ownCloseKey" in fix
    assert "run_id: _ownCloseKey" in fix
    assert "relayed_run_id: _commit.close.run_id" in fix


def test_replacement_precedes_first_close_read(monkeypatch):
    script = _script(monkeypatch)
    fix = script.index("run_id: _ownCloseKey")
    assert fix < script.index("_commit.close?.siblings")


def test_no_script_path_leaves_empty_literal_so_key_guard_applies(monkeypatch):
    script = _script(monkeypatch, script_path=None)
    decl = script[script.index("const _ownCloseKey = ") :]
    assert "?? \"\")" in decl[: decl.index(";")] or "?? ''" in decl[: decl.index(";")]
    assert "if (_ownCloseKey && _commit" in script
