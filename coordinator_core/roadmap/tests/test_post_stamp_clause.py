"""Post-stamp exit-criterion clauses are refused; pre-stamp mechanism clauses pass."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
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
    assert in_process_time_ms(lambda: post_stamp_clause(FOLLOW_ONS))["process_time_ms"] < 50


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


def test_prime_exit_refuses_suite_tier():
    fm = {"prime_exit_criterion": {"statement": "fast tier green", "derived_from": "state/sizings/x.yaml"}}
    v = pg._prime_exit(fm)
    assert v["kind"] == "prime-exit-suite-tier" and "tests covering touched files pass" in v["detail"]
    body = "## Exit criteria\n\n1. The gate refuses X.\n2. The full suite passes.\n"
    fm["prime_exit_criterion"]["statement"] = "the gate refuses X"
    v = pg._prime_exit(fm, None, body)
    assert v["kind"] == "prime-exit-suite-tier" and "body exit criterion 2" in v["detail"]
    assert pg._prime_exit(fm, None, body.replace("The full suite", "Tests covering touched files"))["status"] == "PASS"


SUITE_TIER_PHRASES = (
    "fast tier green",
    "the fast suite passes",
    "full suite",
    "full test suite passes",
    "tier-U",
    "Tier U grant run is green",
    "fast-tier is green",
    "the broad suite passes",
    # A negated mention covers only what it is coordinated with.
    "never the fast tier, and the full suite passes",
    "not the fast tier but the full suite",
    "the full suite or the fast tier passes, never tier-U",
)


@pytest.mark.parametrize("text", SUITE_TIER_PHRASES)
def test_suite_tier_refused(text):
    from coordinator_core.roadmap.post_stamp_clause import suite_tier_refusal

    msg = suite_tier_refusal(text)
    assert msg and "tests covering touched files pass" in msg


@pytest.mark.parametrize(
    "text",
    [
        "tests covering touched files pass",
        "Beat vanilla on category X.",
        "the tests for the gate pass and a refusal names the replacement",
        "a test proves the fast path stays under 50ms",
        "the full plan is reviewed",
        "Targeted tests pass, never the repo's fast tier or full suite.",
        "never the fast tier or the full suite",
        "tests covering touched files pass, not the fast tier nor the broad suite",
        "",
        None,
    ],
)
def test_suite_tier_allowed(text):
    from coordinator_core.roadmap.post_stamp_clause import suite_tier_refusal

    assert suite_tier_refusal(text) is None


@pytest.mark.parametrize("statement", ["fast tier green", "full test suite passes"])
def test_accept_op_refuses_suite_tier_and_accepts_replacement(tmp_path, statement):
    from coordinator_core.ops.sizing_accept_exit_criterion import _handler

    (tmp_path / ".git").mkdir()
    d = tmp_path / "state" / "sizings"
    d.mkdir(parents=True)
    f = d / "s.yaml"
    f.write_text("title: t\nexit_criterion:\n  statement: ok\n")
    r = _handler({"sizing": str(f), "pm_quote": "yes", "statement": statement}, repo_root=tmp_path)
    assert r["exit_code"] == 1 and "tests covering touched files pass" in r["error"]
    assert "fast" not in f.read_text() and "full" not in f.read_text()

    r = _handler(
        {"sizing": str(f), "pm_quote": "yes", "statement": "tests covering touched files pass"},
        repo_root=tmp_path,
    )
    assert "suite tier" not in (r.get("error") or "")
