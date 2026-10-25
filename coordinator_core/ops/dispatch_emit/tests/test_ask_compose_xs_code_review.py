"""An xs-arm script runs the review block when the manifest owes code review and halts review-floor on zero verdicts."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GATE = (
    "if (!_halted && !_reviewReused && !_noReviewPaths && "
    "((_gate.arm !== 'xs' && !_xsTail) || _codeReviewOwed)) {"
)
_HALT = (
    "if (!_halted && _codeReviewOwed && _reviewVerdicts === 0 && _reviewPrep?.product_files !== 0) "
    "{ _halted = { halted: 'review_floor'"
)
_SIDECAR_CLAUSE = "typeof w.sidecar_path === 'string' && w.sidecar_path.trim()"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
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


def test_review_block_runs_when_code_review_owed(monkeypatch):
    script = _script(monkeypatch)
    assert "const _codeReviewOwed = (_manifest?.review_code_paths ?? []).length > 0;" in script
    assert script.index("const _codeReviewOwed") < script.index(_GATE)
    assert "_gate.arm !== 'xs' && !_xsTail && !_noReviewPaths) {\n  phase('Review wave');" not in script


def test_docs_only_xs_keeps_empty_wave(monkeypatch):
    script = _script(monkeypatch)
    assert "if (!_halted && !_codeReviewOwed && (_gate.arm === 'xs' || _xsTail)) { _reviewWave = []; }" in script
    assert "{ _reviewWave = []; }\n  if (!_halted && !_codeReviewOwed && (_gate.arm === 'xs' || _xsTail)) {" in script


def test_zero_verdict_code_run_halts_before_ledger_check(monkeypatch):
    script = _script(monkeypatch)
    assert _HALT in script
    assert script.index("w.verdict === 'BLOCKED'") < script.index(_HALT) < script.index("halted: 'ledger-check'")
    assert "review: 0 verdicts on a code-editing run (" in script


def test_verdict_expression_requires_sidecar_path(monkeypatch):
    script = _script(monkeypatch)
    assert _SIDECAR_CLAUSE in script[script.index("const _reviewVerdicts"):][:300]
    assert _SIDECAR_CLAUSE in script[script.index("receipt: 'review: '"):][:300]


def test_digest_carries_review_receipt(monkeypatch):
    script = _script(monkeypatch)
    assert "receipt: 'review: ' + (Array.isArray(_reviewWave) ? _reviewWave : []).filter(" in script
    assert "_reviewVerdicts" not in script[script.index("receipt: 'review: '"):][:200]


def test_manifest_schema_lists_code_paths_but_does_not_require_them():
    assert "review_code_paths" in ask_compose._MANIFEST_SCHEMA["properties"]
    assert "review_code_paths" not in ask_compose._MANIFEST_SCHEMA["required"]
