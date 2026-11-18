"""The test-leg minutes budget is a one-directional ratchet on box occupancy.

`test_leg_run.TEST_LEG_MINUTES_BUDGET` is the wall-clock bar one halt re-verify or review test leg
is held to. This module fails when an edit raises it above `PINNED_CAP_MINUTES`, when a leg that
overruns its deadline stops reporting `over_budget`, or when a default-composed leg prompt tells
its agent to rerun, background or poll tests instead of running the one engine command. The
`PINNED_CAP_MINUTES` copy is independent: lifting the constant without lifting this one fails.

The budget is wall-clock on purpose: a test leg is bulk work outside DR-344's engine-op scope,
and DR-356 keeps process time as the engine-op axis; this bounds box occupancy, it is not an op
budget.

Negative spec -- what this module does NOT assert: that any real test leg finishes within the
budget (a latency property of the suite it runs).
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops import test_leg_run
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.review_mint.execute_review import _reverify_phase

PINNED_CAP_MINUTES = 10
_LEG_CLI = "python -m coordinator_core.ops.test_leg_run"
_FORBIDDEN = ("alone", "background", "sleep", "poll")


def test_budget_is_at_or_below_the_pinned_cap():
    assert test_leg_run.TEST_LEG_MINUTES_BUDGET <= PINNED_CAP_MINUTES, (
        f"test-leg budget raised to {test_leg_run.TEST_LEG_MINUTES_BUDGET} min, above the pinned "
        f"{PINNED_CAP_MINUTES} min cap. The budget ratchets DOWN only; a leg needing more is a "
        "suite to split. Raising this pair needs a PM ruling."
    )


def test_a_leg_that_exceeds_its_budget_is_reported_over_budget(tmp_path, monkeypatch):
    (tmp_path / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(60)\n", encoding="utf-8"
    )
    out = tmp_path / "out" / "verdict.json"
    monkeypatch.chdir(tmp_path)

    code = test_leg_run.main(["--budget-s", "1", "--out", str(out), "--", "test_slow.py"])

    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert code == test_leg_run.EXIT_TIMEOUT == 3
    assert verdict["over_budget"] is True
    assert verdict["status_after"] == "fail"
    assert verdict["failing_detail"][0]["message"].startswith("timeout:")
    assert verdict["elapsed_s"] < 30


def _halt_reverify_prompt() -> str:
    js = ask_compose._review_recover_js(
        head="H", anchor="A", session_tail="", record_dir="state/warp/r1", run_id="r1"
    )
    at = js.index(f"label: '{ask_compose.HALT_REVERIFY_LABEL}'")
    return js[js.rindex("Halt re-verify leg", 0, at):at]


def _review_test_leg_prompt() -> str:
    return _reverify_phase(
        base_ref="b" * 40, prep_slice_id="prep1", repo_root=None, plan_path="docs/plans/example.md",
        run_base_sha="b" * 40, prompt_head="head", host_degraded=False, sc=lambda call, *_: call,
    )[1]


@pytest.mark.parametrize("compose", [_halt_reverify_prompt, _review_test_leg_prompt])
def test_default_leg_prompt_runs_the_one_engine_command_and_never_loops(compose):
    prompt = compose()
    assert prompt.count(_LEG_CLI) == 1
    lowered = prompt.lower().replace("passed_alone", "")
    for word in _FORBIDDEN:
        assert word not in lowered, f"{compose.__name__} prompt carries {word!r}"
