"""Producer-only wire clauses are refused; consumer-side clauses pass; unowned seams are found."""

from __future__ import annotations

import pytest

from coordinator_core.roadmap.consumer_clause import (
    WIRE_VERBS,
    consumer_clause_refusal,
    producer_only_clause,
    unowned_seams,
)
from coordinator_core.roadmap.post_stamp_clause import (
    EXIT_CRITERION_REFUSAL_HEAD,
    REMEDIATION_MARKER,
)

PLAN_EXIT = (
    "A sizing whose exit criterion names only a producer artefact for a wire/use/feed/read/inject "
    "clause is refused at the sizing gate with a message naming the clause, a producer seam with no "
    "consumer row mints a sizing row owning its wiring, and tests covering touched files pass."
)


def test_refusal_names_the_clause():
    msg = consumer_clause_refusal("the loader writes points.json that feeds the fitter")
    assert msg.startswith(EXIT_CRITERION_REFUSAL_HEAD)
    assert "'feeds the fitter'" in msg
    assert REMEDIATION_MARKER in msg


@pytest.mark.parametrize(
    "text",
    [
        "points.json is read by the fitter",
        "the config is wired into the loader",
        "the adapter injects the token",
        "the renderer uses the cache",
        "the fitter is fed from points.json",
    ],
)
def test_producer_only_refused(text):
    assert producer_only_clause(text)
    assert consumer_clause_refusal(text)


@pytest.mark.parametrize(
    "text",
    [
        "`fitter report --json` shows points.json consumed counts above zero",
        "the loader feeds the fitter and fitter report shows consumed counts above zero",
        "the fitter reads points.json, shown by `fitter report` counts",
        PLAN_EXIT,
        "the exit text lists \"wire\" and \"feed\" as quoted words",
        "a read-only view of the index",
        "",
        None,
    ],
)
def test_consumer_side_or_mention_passes(text):
    assert producer_only_clause(text) is None
    assert consumer_clause_refusal(text) is None


def test_second_clause_is_judged_alone():
    text = "`a report` shows x; the loader feeds the fitter"
    assert producer_only_clause(text) == "feeds the fitter"


def test_wire_verbs_cover_five_stems():
    for stem in ("wire", "use", "feed", "read", "inject"):
        assert stem in WIRE_VERBS


@pytest.mark.parametrize(
    "text,seam",
    [
        ("nothing feeds the fitter", "fitter"),
        ("points.json has no consumer", "points.json"),
        ("there is no consumer row for the mesh export.", "mesh export"),
        ("an unwired fitter input", "fitter input"),
    ],
)
def test_seam_phrases(text, seam):
    out = unowned_seams([text])
    assert [s["seam"] for s in out] == [seam]
    assert out[0]["source"]


def test_seams_dedupe_in_order_of_first_appearance():
    out = unowned_seams(["nothing reads points.json", "unwired mesh", "points.json has no consumer"])
    assert [s["seam"] for s in out] == ["points.json", "mesh"]


def test_row_producing_unnamed_artefact_is_seam():
    rows = [
        {"id": "A1", "statement": "the loader writes points.json", "check": "ls out"},
        {"id": "A2", "statement": "docs are updated", "check": "git diff"},
    ]
    out = unowned_seams([], rows)
    assert [s["seam"] for s in out] == ["points.json"]


def test_row_artefact_named_by_another_row_is_owned():
    rows = [
        {"id": "A1", "statement": "the loader writes points.json", "check": "ls out"},
        {"id": "A2", "statement": "the fitter consumes points.json", "check": "fitter report"},
    ]
    assert unowned_seams([], rows) == []


def test_row_artefact_named_in_other_rows_check_is_owned():
    rows = [
        {"id": "A1", "statement": "the loader emits points.json", "check": "ls"},
        {"id": "A2", "statement": "fitter consumes loader output", "check": "fitter report points.json"},
    ]
    assert unowned_seams([], rows) == []


def test_non_artefact_production_is_not_a_seam():
    assert unowned_seams([], [{"id": "A1", "statement": "the script writes tests"}]) == []


def test_empty_inputs():
    assert unowned_seams([]) == []
    assert unowned_seams([None, ""], None) == []
