"""The emitted family leg: one family attempt, one claude fallback, both results kept.

Assertions read the emitted JS text; the leg is not executed here."""
from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, stage_family, stage_family_binding
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_RUN = "run-1"
_OPTED = {"stage_family_binding": stage_family_binding.BINDING_REF}


def _script(monkeypatch, sizing: dict | None, sizing_rel: str | None = "sizing.yaml") -> str:
    monkeypatch.setattr(ask_compose, "load_sizing", lambda root, rel: sizing)
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None if sizing_rel else "add a thing", sizing_rel=sizing_rel, run_id=_RUN, session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", **REVIEW_KW,
    )


def _family_fn(script: str) -> str:
    start = script.index("  async function _familyRun(r) {")
    return script[start : script.index("\n  }\n", start)]


def test_opted_in_script_carries_the_family_arm_in_the_row_loop_and_the_late_loop(monkeypatch):
    script = _script(monkeypatch, _OPTED)
    assert script.count("r.family ? _familyRun(r) : ") == 2
    late = script[script.index("for (const r of _lateRows) {"):]
    assert "async () => r.family ? _familyRun(r) : r.group ? _groupRun(r)" in late[:400]
    row_loop = script[script.index("for (const r of _manifest.rows.filter((x) => x.wave === w))"):]
    assert "r.kept_from ? _keptRun(r) : r.family ? _familyRun(r) : r.group ? _groupRun(r)" in row_loop


def test_non_opted_in_script_carries_no_family_bytes(monkeypatch):
    for sizing, rel in (({}, "sizing.yaml"), ({"stage_family_binding": ""}, "sizing.yaml")):
        script = _script(monkeypatch, sizing, rel)
        assert "_familyRun" not in script and "_FAMILY_RESULT_SCHEMA" not in script


def test_non_opted_in_script_equals_the_script_with_the_leg_disabled(monkeypatch):
    plain = _script(monkeypatch, {})
    monkeypatch.setattr(ask_compose, "_family_enabled", lambda *a: False)
    assert _script(monkeypatch, _OPTED) == plain


def test_foreign_binding_ref_keeps_the_leg_and_is_refused_at_stage_time(monkeypatch):
    assert "async function _familyRun(r)" in _script(monkeypatch, {"stage_family_binding": "elsewhere"})


def test_family_leg_reruns_on_claude_only_for_blocked_failed_or_unresolved(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    for verdict in stage_family.FALLBACK_VERDICTS:
        assert f"_fam.verdict === '{verdict}'" in fn
    assert "'OK'" in fn and "OK" not in stage_family.FALLBACK_VERDICTS
    # the family attempt runs once, on the runner; the fallback once, on the claude brief
    assert fn.count("await agent(") == 2
    assert "agentType: r.agent_type" in fn and "agentType: r.fallback_agent_type" in fn
    assert "brief_path: r.fallback_brief_path" in fn
    assert "schema: _FAMILY_RESULT_SCHEMA" in fn


def test_unparseable_reply_synthesizes_the_failed_family_attempt_before_the_fallback(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    synth = fn.index("if (!_fam) _fam = { family: r.family, stage: r.stage_role, exit_code: -1, output_path: '', verdict: 'FAILED', blocked_reason: 'family-stage-runner reply missing or unparseable' }")
    assert synth < fn.index("r.fallback_agent_type")


def test_record_keys_match_stage_family_stage_record(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    fam = {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "scratch/x", "verdict": "FAILED", "blocked_reason": "out of credits"}
    fb = {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "", "verdict": "OK"}
    rec = stage_family.stage_record("research", fam, fb)
    for key in rec:
        assert re.search(rf"\b{key}:", fn), key
    assert "fallback_for: _fam.family" in fn
    assert rec["attempts"][1]["fallback_for"] == "agy" and rec["reported_family"] == "claude"
    # a result that kept the family never claims claude; one that fell back never claims the family
    assert "reported_family: _fam.family" in fn
    assert "reported_family: 'claude'" in fn or 'reported_family: "claude"' in fn
    schema_keys = re.search(r"_FAMILY_RESULT_SCHEMA = (\{.*\});", _script(monkeypatch, _OPTED)).group(1)
    for key in stage_family._RESULT_KEYS:
        assert f'"{key}"' in schema_keys


def test_fallback_failure_is_blocked_and_still_keeps_both_attempts(monkeypatch):
    fn = _family_fn(_script(monkeypatch, _OPTED))
    assert "(_fbOk ? 'DONE: ' : 'BLOCKED: ')" in fn
    assert "attempts: [_fam, { ..._fb, fallback_for: _fam.family }]" in fn


@pytest.mark.parametrize("bad", ["", None])
def test_bare_prompt_run_carries_the_leg_because_its_sizing_is_minted_at_run_time(monkeypatch, bad):
    script = _script(monkeypatch, _OPTED, sizing_rel=bad)
    assert "async function _familyRun(r)" in script
    assert script.count("r.family ? _familyRun(r) : ") == 2


def test_unminted_sizing_path_carries_the_leg(monkeypatch):
    def _missing(root, rel):
        raise FileNotFoundError(rel)

    monkeypatch.setattr(ask_compose, "load_sizing", _missing)
    assert ask_compose._family_enabled("REPO", "sizing.yaml") is True
