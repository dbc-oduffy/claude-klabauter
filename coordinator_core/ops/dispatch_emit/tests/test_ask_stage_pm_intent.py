"""ask_stage writes the PM's verbatim words, never the EM's paraphrase, into every brief."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_TEXT_FILE,
    PM_INTENT_FILE,
    RELAYED_ASK_FILE,
    RELAYED_ASK_HEADING,
    RECORD_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_stage import (
    PM_INTENT_HEADING,
    pm_intent_block,
    relayed_ask_block,
    stage,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN_REL, SIZING_REL, repo  # noqa: F401

_VERBATIM = "make the XS arm commit in-run\nand say so in the docs"


def _sizing(repo, **over):  # noqa: F811
    doc = {
        "intent": "EM restatement: tidy the commit path",
        "intent_source": "em-elaborated",
        "pm_verbatims": [{"turn": 0, "text": _VERBATIM}],
        "exit_criterion": {"statement": "an XS run lands its own commit, because the EM should not loop"},
        "deliverable_id": "dlv-xs-abc123",
        **over,
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_every_xs_brief_opens_with_the_verbatims_and_criterion_not_the_paraphrase(repo):  # noqa: F811
    _sizing(repo)
    m = stage(repo, run_id="x1run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    brief = (repo / m.rows[0].brief_path).read_text(encoding="utf-8")
    assert brief.startswith(PM_INTENT_HEADING)
    assert "> make the XS arm commit in-run\n> and say so in the docs" in brief
    assert "because the EM should not loop" in brief
    head = brief.split("these words win", 1)[0]
    assert "EM restatement" not in head
    assert (repo / RECORD_DIR_ROOT / "x1run" / PM_INTENT_FILE).read_text(encoding="utf-8") in brief


def test_the_raw_ask_as_submitted_joins_the_block(repo):  # noqa: F811
    _sizing(repo, pm_verbatims=[])
    run_dir = repo / RECORD_DIR_ROOT / "x2run"
    run_dir.mkdir(parents=True)
    (run_dir / ASK_TEXT_FILE).write_text("fix it all, literally", encoding="utf-8")
    m = stage(repo, run_id="x2run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert "> fix it all, literally" in (repo / m.rows[0].brief_path).read_text(encoding="utf-8")


def test_a_plan_brief_carries_its_cited_sizings_verbatims(repo):  # noqa: F811
    _sizing(repo)
    plan = (repo / PLAN_REL).read_text(encoding="utf-8").replace(
        "deliverable_id: dlv-fixture-abc123", f"deliverable_id: dlv-fixture-abc123\nsizing_object: {SIZING_REL}"
    )
    (repo / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    for row in m.rows:
        assert PM_INTENT_HEADING in (repo / row.brief_path).read_text(encoding="utf-8")


def test_relayed_text_is_its_own_block_below_the_pm_intent_and_not_in_it(repo):  # noqa: F811
    _sizing(repo)
    run_dir = repo / RECORD_DIR_ROOT / "x3run"
    run_dir.mkdir(parents=True)
    (run_dir / RELAYED_ASK_FILE).write_text("RELAYED-MARK fix it", encoding="utf-8")
    m = stage(repo, run_id="x3run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    brief = (repo / m.rows[0].brief_path).read_text(encoding="utf-8")
    pm_intent = (run_dir / PM_INTENT_FILE).read_text(encoding="utf-8")
    assert "RELAYED-MARK" not in pm_intent
    assert brief.index(PM_INTENT_HEADING) < brief.index(RELAYED_ASK_HEADING)
    assert brief.startswith(pm_intent)
    assert "> RELAYED-MARK fix it" in brief
    assert "outrank it" in brief


def test_a_relayed_run_with_no_pm_words_has_only_the_relayed_block(repo):  # noqa: F811
    _sizing(repo, pm_verbatims=[])
    run_dir = repo / RECORD_DIR_ROOT / "x4run"
    run_dir.mkdir(parents=True)
    (run_dir / RELAYED_ASK_FILE).write_text("RELAYED-MARK fix it", encoding="utf-8")
    m = stage(repo, run_id="x4run", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    brief = (repo / m.rows[0].brief_path).read_text(encoding="utf-8")
    assert brief.startswith(RELAYED_ASK_HEADING) and PM_INTENT_HEADING not in brief
    assert not (run_dir / PM_INTENT_FILE).exists()


def test_pm_intent_block_is_not_given_relayed_text():
    assert pm_intent_block({}, None) == ""
    assert relayed_ask_block("  ") == ""


def test_no_pm_words_on_record_adds_nothing():
    assert pm_intent_block({"intent": "x", "intent_source": "em-elaborated"}, None) == ""
    assert "> x" in pm_intent_block({"intent": "x", "intent_source": "pm-verbatim"}, None)
