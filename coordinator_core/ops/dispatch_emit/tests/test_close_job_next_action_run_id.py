"""The close_job next_action carries the close block's workflow-suffixed run id; land_run accepts both forms."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, run_close
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
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
        script_path="scratch/warp/run-1.workflow.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_close_job_next_action_run_id_is_the_close_blocks(monkeypatch):
    script = _script(monkeypatch)
    at = script.index("_nextAction = { kind: 'close_job'")
    head = script[at : script.index("close:", at)]
    assert "run_id: _commit.close?.run_id ?? _runId" in head
    assert "params: { run_id: _runId," not in head


def test_resolver_accepts_bare_and_suffixed_ids(tmp_path, monkeypatch):
    seen = {"run-1.workflow"}
    monkeypatch.setattr(run_close, "_load", lambda _root, stem: {} if stem in seen else None)
    assert run_close.resolve_close_job_stem(tmp_path, "run-1") == "run-1.workflow"
    assert run_close.resolve_close_job_stem(tmp_path, "run-1.workflow") == "run-1.workflow"
    assert run_close.resolve_close_job_stem(tmp_path, "other") is None
