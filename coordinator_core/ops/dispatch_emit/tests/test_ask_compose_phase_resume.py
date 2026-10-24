"""compose_ask_script phase resume: resume_phases rides the gate and the emitted phases skip reused work."""

from __future__ import annotations

import coordinator_core.ops.dispatch_emit.ask_compose as ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.phase_ledger import RESUME_PHASES_FIELD
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose():
    return compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_resume_phases_rides_every_gate_take_and_the_reply_schema():
    script = _compose()
    assert RESUME_PHASES_FIELD == "resume_phases"
    assert "resume_phases: _scouted.resume_phases ?? null" in script
    assert "resume_phases: _reworded.resume_phases ?? null" in script
    assert script.count("resume_phases: _apm.resume_phases ?? null") == 1
    assert script.count("resume_phases: _xl.resume_phases ?? null") == 1
    assert ask_compose._GATE_SCHEMA["properties"][RESUME_PHASES_FIELD] == {"type": ["object", "null"]}


def test_reuse_helper_reads_the_gate_and_replays_verdicts():
    script = _compose()
    assert "const _reusePhase = (n) => !_halted && Array.isArray(_gate.resume_phases?.reuse)" in script
    assert "_gate.resume_phases?.verdicts?.[n] ?? null" in script
    assert script.index("const _reusePhase") < script.index("phase('plan')")


def test_plan_review_and_rulings_skip_on_reuse():
    script = _compose()
    assert "(_reusePhase('plan') && _reusePhase('plan_review'))) && _gate.resume_plan" in script
    reuse = script.index("_gate.resume_plan && _reusePhase('plan_gate_rulings')")
    rule_call = script.index("apply-rulings", reuse)
    assert "resume_rulings: (_phaseVerdict('plan_gate_rulings')?.applied ?? [])" in script[reuse:rule_call]
    assert "else if (_gate.resume_plan && (_gate.pending_rulings == null" in script[reuse:rule_call]


def test_review_phase_reused_skips_the_wave_and_seeds_its_verdict():
    script = _compose()
    assert "const _reviewReused = _reusePhase('review') && !!_phaseVerdict('review');" in script
    assert "_reviewWave = Array.isArray(_rv0.wave) ? _rv0.wave : []" in script
    assert "if (!_halted && !_reviewReused && _gate.arm !== 'xs'" in script


def test_review_phase_rerun_keeps_non_blocked_slices_and_the_rerun_wins():
    script = _compose()
    seed = script.index("const _reviewSeed =")
    merge = script.index("if (_reviewSeed.length && Array.isArray(_reviewWave))")
    assert "w.verdict === 'BLOCKED' && (w.findings ?? 0) > (w.applied ?? 0)" in script[seed:merge]
    assert "!_rerun.has(w.slice_id)" in script[merge:merge + 400]
    assert merge < script.index("const _blockedSlices")
