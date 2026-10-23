"""Per-row `verify:` composes only where no terminal test leg already decides the result."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.emit import _run_row_helper_js, compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW

_PUSH = "label: 'verify:' + id"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _emit_script(tmp_path):
    target = tmp_path / "pkg" / "c1.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")
    test_target = tmp_path / "pkg" / "tests" / "test_c1.py"
    test_target.parent.mkdir(parents=True)
    test_target.write_text("def test_x(): pass\n", encoding="utf-8")
    row = WaveRow(id="C1", title="t", surface="pkg", writes=["pkg/c1.py"], reads=[], depends_on=[])
    return compose_script([[row]], name="wf", description="d", repo_root=tmp_path, **REVIEW_KW)


def _ask_script():
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


def test_an_emit_script_with_a_terminal_test_leg_composes_no_verify_label(tmp_path):
    script = _emit_script(tmp_path)

    assert "test:terminal" in script
    assert _PUSH not in script
    assert "const _verifications = [];" in script
    assert "await Promise.all(_verifications);" in script


def test_the_row_helper_keeps_the_push_where_no_terminal_leg_composes():
    assert _PUSH in _run_row_helper_js()
    assert "if (!incomplete && verifyScope && (true))" in _run_row_helper_js()
    assert _PUSH not in _run_row_helper_js(None, None, "false")


def test_the_ask_row_runner_pushes_behind_the_review_independent_test_guard():
    script = _ask_script()

    assert "if (!incomplete && verifyScope && (_rowVerifyWanted))" in script
    decl = (
        "const _rowVerifyWanted = "
        "!(_gate.arm !== 'xs' && !_xsTail && (_manifest.test_targets ?? []).length);"
    )
    assert decl in script
    assert script.index(decl) < script.index("_rows[r.id] = _runRow(")


def test_the_executor_brief_names_scoped_test_legs_not_per_row_verify_calls():
    row = WaveRow(id="C1", title="t", surface="pkg", writes=["pkg/c1.py"], reads=[], depends_on=[])
    script = compose_script(
        [[row]], name="wf", description="d", plan_path="docs/plans/example.md", **REVIEW_KW
    )

    assert "its scoped test legs" in script
    assert "per-row `verify:`" not in script
