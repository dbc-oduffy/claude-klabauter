"""A launcher child's render resumes past stage when its staged twin's inline slot carries a resume block.

Render-level only: the emitted bytes are inspected, no node run. The node-level falsifier is the incident test."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import _BLITZ_FN, _SIZING

_GUARD_OPEN = "  if (!_STAGED_RESUME) {\n  phase('size');"


def _script(monkeypatch, *, launcher_child: bool, raw: bool, arm: str = "s") -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt="do the thing" if raw else None, sizing_rel=None if raw else _SIZING,
        run_id="run-1", session_id=None, wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="scratch/warp/x.mjs", in_run_commit=True,
        launcher_child=launcher_child, **REVIEW_KW,
    )


def _label_index(script: str, label: str) -> list[int]:
    return [m.start() for m in re.finditer(r"label: '" + re.escape(label) + r"'", script)]


@pytest.mark.parametrize("raw", [True, False])
def test_a_standalone_emit_holds_no_resume_guard(monkeypatch, raw):
    script = _script(monkeypatch, launcher_child=False, raw=raw)
    assert "_STAGED_RESUME" not in script


@pytest.mark.parametrize("raw", [True, False])
def test_a_launcher_child_emit_declares_the_resume_once_from_the_inline_slot(monkeypatch, raw):
    script = _script(monkeypatch, launcher_child=True, raw=raw)
    assert script.count("const _STAGED_RESUME = ") == 1
    decl = script.index("const _STAGED_RESUME = ")
    assert script.index("const _STAGED_INLINE = ") < decl
    assert "s.resume" in script[decl:decl + 400]
    assert "typeof r.arm === 'string'" in script[decl:decl + 400]
    assert "_STAGED_RESUME" not in script[: script.index("const _STAGED_INLINE = ")]


def test_the_raw_ask_size_block_and_every_size_leg_sit_inside_the_resume_guard(monkeypatch):
    script = _script(monkeypatch, launcher_child=True, raw=True)
    assert script.count(_GUARD_OPEN) == 1
    start = script.index(_GUARD_OPEN)
    end = script.index("  phase('gate');")
    assert start < end
    for label in ("size", "size-retry", "size-reword"):
        spots = _label_index(script, label)
        assert spots, label
        assert all(start < i < end for i in spots), label
    block = script[start:end]
    assert block.rstrip().endswith("}")
    assert not re.search(r"const (_scouted|_scoutClean|_pmExit)\b", script)
    assert "let _scouted = null;" in script[:start]


def test_the_gate_is_seeded_from_the_resume_block_and_its_leg_never_runs_on_resume(monkeypatch):
    raw = _script(monkeypatch, launcher_child=True, raw=True)
    assert "let _gate = _STAGED_RESUME ? { arm: _STAGED_RESUME.arm, halt: null," in raw
    sizing = _script(monkeypatch, launcher_child=True, raw=False)
    assert "let _gate = _STAGED_RESUME ? { arm: _STAGED_RESUME.arm, halt: null," in sizing
    (gate_leg,) = _label_index(sizing, "gate")
    seed = sizing.index("let _gate = _STAGED_RESUME ?")
    assert seed < gate_leg < sizing.index("\n", seed)


@pytest.mark.parametrize("raw", [True, False])
def test_every_later_pre_stage_leg_is_gated_on_the_resume(monkeypatch, raw):
    script = _script(monkeypatch, launcher_child=True, raw=raw, arm="m_plus")
    assert "if (!_halted && !_STAGED_RESUME && _gate.arm === 's') {" in script
    assert "if (!_halted && !_STAGED_RESUME && _gate.arm === 'm_plus') {" in script
    if raw:
        assert "_round < 3 && !_STAGED_RESUME && _gate.halt" in script
    for label in ("plan", "plan-review", "plan-revise", "xl-exit"):
        for i in _label_index(script, label):
            before = script[:i]
            guard = max(before.rfind("!_STAGED_RESUME &&"), before.rfind("  if (!_STAGED_RESUME) {"))
            assert guard != -1, label


def test_the_resume_seeds_the_manifest_through_the_existing_adopt_so_a_failed_verify_halts_by_name(monkeypatch):
    script = _script(monkeypatch, launcher_child=True, raw=True)
    assert "await _adoptStageReply({ manifest_handle: _STAGED_INLINE.handle });" in script
    seed = script.index("await _adoptStageReply({ manifest_handle: _STAGED_INLINE.handle });")
    assert seed < script.index(_GUARD_OPEN)
    assert "stage reply sidecar does not match its handle: " in script
    for field in ("_sizingRel = _STAGED_RESUME.sizing_rel", "_planRel = _STAGED_RESUME.plan_rel"):
        assert field in script


def test_the_guard_keys_on_the_resume_block_never_on_the_arm(monkeypatch):
    for arm in ("xs", "s", "m_plus"):
        script = _script(monkeypatch, launcher_child=True, raw=True, arm=arm)
        assert script.count(_GUARD_OPEN) == 1
        assert not re.search(r"_STAGED_RESUME\.arm\s*===\s*'(?:xs|s|m_plus)'\s*\)\s*\{\s*phase\('size'", script)
