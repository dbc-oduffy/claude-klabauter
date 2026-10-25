"""A run whose rows declare no path skips the review wave instead of halting at review prep."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
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
        script_path="x.mjs",
        **REVIEW_KW,
    )


def test_review_block_gated_on_declared_paths(monkeypatch):
    script = _script(monkeypatch)
    assert "const _noReviewPaths = !_reviewPaths.length;" in script
    assert "!!((_manifest?.rows ?? []).length &&" not in script
    # e312728c2c (phase reuse) added the `!_reviewReused` guard to the review block.
    assert "if (!_halted && !_reviewReused && _gate.arm !== 'xs' && !_xsTail && !_noReviewPaths) {\n  phase('Review wave');" in script


def test_no_path_run_logs_and_empties_wave(monkeypatch):
    script = _script(monkeypatch)
    assert "execute_review skipped: no row declares a path, nothing to review" in script
    skip_line = script.split("execute_review skipped")[0].rsplit("\n", 1)[-1]
    assert "!_halted && _noReviewPaths && !_xsTail && _gate.arm !== 'xs'" in skip_line
    assert script.split("execute_review skipped")[1].split("\n", 1)[0].endswith("_reviewWave = []; }")


def test_refire_remaining_leg_writes_count_as_review_paths(monkeypatch):
    script = _script(monkeypatch)
    assert (
        "const _reviewPaths = [...(_manifest?.review_declared_paths ?? []), "
        "...(_gate.resume_ledger?.remaining_leg?.writes ?? [])];"
    ) in script
    assert script.index("const _reviewPaths") < script.index("const _noReviewPaths")
    assert script.index("const _noReviewPaths") < script.index("phase('Review wave')")
    assert "_manifest.review_declared_paths" not in script.split("const _noReviewPaths")[1].split("execute_review skipped")[0]
