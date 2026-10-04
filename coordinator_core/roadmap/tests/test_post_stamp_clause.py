"""Post-stamp exit-criterion clauses are refused; pre-stamp mechanism clauses pass."""

from __future__ import annotations

import time

import pytest

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.post_stamp_clause import post_stamp_clause, post_stamp_refusal

FOLLOW_ONS = (
    "A warp run on this sizing reaches dispatch.terminal_commit with the DoE-gated row "
    "listed incomplete; after terminal_commit the index equals HEAD; a warp-minted baton "
    "carries a cascade-eligible kind and the sizing reaches shipped through the cascade; "
    "cascade_terminal measures under 200ms process time"
)


@pytest.mark.parametrize(
    "text",
    [
        "the sizing reaches shipped",
        "This plan is stamped implemented",
        "its sizing becomes shipped",
        "the plan's own baton flips to closed",
        "the deliverable cascade fires",
        FOLLOW_ONS,
    ],
)
def test_refused(text):
    assert post_stamp_clause(text)
    assert "pre-stamp mechanism" in post_stamp_refusal(text)


@pytest.mark.parametrize(
    "text",
    [
        "a plan cannot reach `status: implemented` without a review receipt",
        "a baton has one of three outcomes: shipped, closed or continued",
        "the fixture plan is stamped implemented and the cascade fired",
        "any sizing reaches shipped through the cascade in the test",
        "the plan never becomes implemented until the receipt lands",
        "a test proves the cascade would fire for a cascade-eligible baton kind",
        "cascade_terminal measures under 200ms process time",
    ],
)
def test_allowed(text):
    assert post_stamp_refusal(text) is None


def test_prime_exit_defect():
    fm = {"prime_exit_criterion": {"statement": "the sizing reaches shipped", "derived_from": "x"}}
    v = pg._prime_exit(fm)
    assert v["kind"] == "prime-exit-post-stamp"


def test_accept_op_refuses(tmp_path):
    from coordinator_core.ops.sizing_accept_exit_criterion import _handler

    (tmp_path / ".git").mkdir()
    d = tmp_path / "state" / "sizings"
    d.mkdir(parents=True)
    f = d / "s.yaml"
    f.write_text("title: t\nexit_criterion:\n  statement: ok\n")
    r = _handler(
        {"sizing": str(f), "pm_quote": "yes", "statement": FOLLOW_ONS}, repo_root=tmp_path
    )
    assert r["exit_code"] == 1 and "pre-stamp mechanism" in r["error"]


def test_under_50ms():
    t = time.perf_counter()
    post_stamp_clause(FOLLOW_ONS)
    assert time.perf_counter() - t < 0.05


_BODY = """## Exit criteria — verification

1. The targeted tests are green.
2. The sizing reaches `shipped` through the cascade after DoE lands.

## Next
"""


def test_body_exit_list_is_gated():
    from coordinator_core.roadmap.post_stamp_clause import exit_criteria_items, post_stamp_body_refusal

    assert len(exit_criteria_items(_BODY)) == 2
    assert "body exit criterion 2" in post_stamp_body_refusal(_BODY)
    assert post_stamp_body_refusal(_BODY.replace("The sizing reaches `shipped`", "a test proves")) is None


def test_prime_exit_refuses_body_item():
    fm = {"prime_exit_criterion": {"statement": "the gate refuses X", "derived_from": "state/sizings/x.yaml"}}
    assert pg._prime_exit(fm, None, _BODY)["kind"] == "prime-exit-post-stamp"
    assert pg._prime_exit(fm, None, None)["status"] == "PASS"
