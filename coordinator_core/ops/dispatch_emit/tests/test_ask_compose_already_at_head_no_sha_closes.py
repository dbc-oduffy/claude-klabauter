"""An all-executors already-at-HEAD note set with no sha closes in-run through the op, never halting NO_SLICES."""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD_OPEN = "  if (!_halted && (!_reviewPrep ||"
_LANDED = "348c80115d"
_NO_SHA = "r1: DONE (nothing changed)"


def _script() -> str:
    return compose_ask_script(
        repo_root="REPO",
        prompt="do it",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _hook(script: str) -> str:
    start = script.index("  async function _alreadyDeliveredClose() {")
    return script[start : script.index("\n  }\n", start) + 4]


def test_hook_carries_the_no_sha_form_and_sends_no_sha_when_none_named() -> None:
    hook = _hook(_script())
    assert "nothing changed|already at HEAD" in hook
    assert "_sha ? { sha: _sha } : {}" in hook
    assert "_r.sha" in hook


_node = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


def test_the_close_judges_the_write_set_only_after_every_leg_settles() -> None:
    """The in-run close reads the worktree; a leg still writing would make a dirty path read clean."""
    script = _script()
    rows = script.rfind("await Promise.all(Object.values(_rows));")
    assert 0 <= rows < script.index("await _alreadyDeliveredClose()")
