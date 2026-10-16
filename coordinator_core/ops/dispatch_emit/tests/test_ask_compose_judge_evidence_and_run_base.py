"""The composed verify judge names the run plan's evidence sidecar, and a caller-supplied run base replaces HEAD."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _JUDGE_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_EVIDENCE_JS = "'.evidence.yaml'"


def _script(monkeypatch, **extra) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    monkeypatch.setattr(ask_compose, "head_sha", lambda _root: "h" * 40)
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **_JUDGE_KW,
        **extra,
    )


def test_the_verify_prompt_names_the_plans_evidence_sidecar(monkeypatch):
    script = _script(monkeypatch)
    verify = script.index("label: 'verify'")
    assert "row_evidence: " in script
    assert script.count(_EVIDENCE_JS) >= 1
    assert "EVIDENCE_PATH_SLOT_X" not in script
    assert "PLAN_PATH_SLOT_X" not in script
    # One script serves a fresh and a resume_ledger gate: both bind _planRel before the one verify call.
    assert script.index("_planRel = _gate.resume_ledger.plan") < verify
    assert script.count("row_evidence: ") == 1


def test_without_a_caller_base_the_base_is_head(monkeypatch):
    script = _script(monkeypatch)
    assert "h" * 40 in script


def test_a_caller_run_base_is_the_only_base(monkeypatch):
    script = _script(monkeypatch, run_base_sha="a" * 40)
    assert "a" * 40 in script
    assert "h" * 40 not in script
