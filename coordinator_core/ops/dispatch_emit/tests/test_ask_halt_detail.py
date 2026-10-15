"""The sizing-assemble halt line carries both assemble streams in `detail`, even when stderr is empty."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW


def _detail_expr() -> str:
    script = compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )
    line = next(ln for ln in script.splitlines() if "sizing-assemble failed" in ln)
    return re.search(r"detail: (.*), run_id: _runId", line).group(1)


def test_detail_joins_both_streams_instead_of_falling_through_on_empty_stderr():
    expr = _detail_expr()
    assert "_assembled.stderr" in expr and "_assembled.stdout" in expr
    assert ".join(" in expr
    assert "stderr ??" not in expr


def test_detail_drops_empty_streams_before_joining():
    assert ".filter(" in _detail_expr()
