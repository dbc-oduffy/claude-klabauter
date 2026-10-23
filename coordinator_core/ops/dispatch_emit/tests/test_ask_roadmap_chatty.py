"""roadmap arm: the roadmapBlitz call carries the chatty review keys only when plan_blitz_args does."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit.ask_roadmap import roadmap_stage_lines

REVIEW = {"memberBriefs": {"review-1": "a {PEERS}", "review-2": "b"}, "overseerPrompt": "o"}


def _rb_line(plan_blitz_args):
    lines = roadmap_stage_lines(
        repo_root="/r", run_id="run1", interaction_mode="pm",
        plan_blitz_args=plan_blitz_args,
    )
    return next(ln for ln in lines if "await roadmapBlitz(" in ln)


def test_chatty_adds_both_keys_to_roadmap_call():
    line = _rb_line({"chatty": True, "chattyReview": REVIEW})
    assert "chatty: true, " in line
    assert f"chattyReview: {json.dumps(REVIEW, sort_keys=True)}, " in line


def test_without_chatty_roadmap_lines_unchanged():
    base = roadmap_stage_lines(repo_root="/r", run_id="run1", interaction_mode="pm", plan_blitz_args=None)
    other = roadmap_stage_lines(
        repo_root="/r", run_id="run1", interaction_mode="pm", plan_blitz_args={"mode": "x"}
    )
    assert "chatty" not in _rb_line({"mode": "x"})
    assert [ln for ln in base if "roadmapBlitz(" in ln] == [ln for ln in other if "roadmapBlitz(" in ln]
    assert "chatty" not in "".join(base)


def test_chatty_false_or_missing_review_adds_nothing():
    assert "chatty:" not in _rb_line({"chatty": False, "chattyReview": REVIEW})
    assert "chatty:" not in _rb_line({"chatty": True})
