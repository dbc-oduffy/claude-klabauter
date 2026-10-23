"""A zero-row memo-close manifest never pre-empts the ledger-replan / ledger-remaining legs of an M+ ledger resume.

Asserts on composed text: the legs run ahead of (replan) or independently of (remaining) the
`_memoCloseOnly` shortcut, and a resume without resume_ledger keeps the shortcut.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_MEMO_SET = "_memoCloseOnly = true; _allLanded = true; _xsTail = true;"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def test_the_ledger_legs_run_before_the_ledger_verdict_and_replan_precedes_the_memo_shortcut(monkeypatch):
    script = _script(monkeypatch)
    replan = script.index("label: 'ledger-replan'")
    memo = script.index(_MEMO_SET)
    remaining = script.index("label: 'ledger-remaining'")
    verify = script.index("label: 'verify'")
    assert replan < memo < remaining < verify


def test_the_ledger_remaining_leg_condition_does_not_read_the_memo_close_flag(monkeypatch):
    script = _script(monkeypatch)
    at = script.index("if (_resumeLedger && !_ledgerExec && ((")
    cond = script[at : script.index("{", at + 10)]
    assert "_memoCloseOnly" not in cond and "_allLanded " not in cond


def test_the_replan_leg_runs_for_an_owing_resume_whatever_the_manifest_holds(monkeypatch):
    script = _script(monkeypatch)
    at = script.index("if (_ledgerOwes && !_stageRefusal)")
    assert "_memoCloseOnly" not in script[at : script.index("label: 'ledger-replan'")]


def test_the_last_wave_takes_no_checkpoint_leg(monkeypatch):
    script = _script(monkeypatch)
    assert "if (w !== _waves[_waves.length - 1]) await _commitWave(w, _waveIds);" in script
    assert script.count("await _commitWave(w, _waveIds)") == 1


def test_a_resume_without_a_ledger_keeps_the_memo_close_only_tail(monkeypatch):
    script = _script(monkeypatch)
    assert script.count(_MEMO_SET) == 1
    assert "const _resumeLedger = !_halted && !!_gate.resume_ledger;" in script
    at = script.index(_MEMO_SET)
    assert "!(_manifest.rows ?? []).length" in script[at - 400 : at]
