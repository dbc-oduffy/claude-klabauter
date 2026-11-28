"""Tests for the deferral challenger and the EM flag queue writer."""
from __future__ import annotations

import json

import pytest

from coordinator_core.rulings.challenge import challenge
from coordinator_core.rulings.em_flags import append_flag, queue_path


def _one(surface, text, fields):
    out = challenge(surface, text, fields)
    assert len(out["verdicts"]) == 1, out
    return out, out["verdicts"][0]


def test_bare_not_now_is_challenge_with_null_class():
    out, v = _one("reviewer_unapplied", "", {"unapplied_reason": "not now"})
    assert v["verdict"] == "challenge" and v["reason_class"] is None and v["kind"] == "deferral"
    assert out["flag_em"] is True


def test_reviewer_text_and_field_both_challenged_fields_first():
    out = challenge(
        "reviewer_unapplied", "Not now; follow-up later.", {"unapplied_reason": "not now"}
    )
    assert [v["verdict"] for v in out["verdicts"]] == ["challenge", "challenge"]
    assert out["verdicts"][0]["span"].startswith("fields.unapplied_reason")
    assert out["verdicts"][1]["span"].startswith("reviewer_unapplied.text")


@pytest.mark.parametrize(
    "reason,cls",
    [
        ("another repo: coordinator-content-repo owns hook wiring", "another_repo"),
        ("plan-sized: see docs/plans/2026-11/2026-11-28-foo-abc123.md", "plan_sized"),
        ("plan-sized, tracked as pln-foo-bar-123456", "plan_sized"),
        ("irreversible: force-push to the shared remote", "irreversible"),
        ("tradeoff: latency vs memory", "tradeoff"),
        ("traded against throughput: accuracy", "tradeoff"),
        ("tradeoff between startup time and cache size", "tradeoff"),
    ],
)
def test_substantiated_class_passes(reason, cls):
    out, v = _one("reviewer_unapplied", "", {"unapplied_reason": reason})
    assert v["verdict"] == "pass" and v["reason_class"] == cls and v["kind"] == "named_reason"
    assert out["flag_em"] is False


@pytest.mark.parametrize(
    "reason,cls,missing",
    [
        ("another repo", "another_repo", "repo"),
        ("this is plan-sized", "plan_sized", "plan"),
        ("irreversible", "irreversible", "act"),
        ("tradeoff", "tradeoff", "sides"),
        ("tradeoff vs", "tradeoff", "sides"),
        ("cross-repo", "another_repo", "repo"),
    ],
)
def test_unsubstantiated_class_challenges_and_names_missing_element(reason, cls, missing):
    out, v = _one("reviewer_unapplied", "", {"unapplied_reason": reason})
    assert v["verdict"] == "challenge" and v["reason_class"] == cls
    assert missing in v["why"]
    assert out["flag_em"] is True


def test_agent_report_bare_plan_sized_challenges():
    out, v = _one("agent_report", "Left as is because it is plan-sized.", {})
    assert v["verdict"] == "challenge" and v["reason_class"] == "plan_sized"
    assert out["flag_em"] is True


def test_pm_handoff_passes_with_question_and_challenges_without():
    _, ok = _one("plan", "Open question: should the cache be per-session?", {})
    assert ok["verdict"] == "pass" and ok["reason_class"] is None
    _, bad = _one("plan", "TBD by the PM.", {})
    assert bad["verdict"] == "challenge" and bad["reason_class"] is None


def test_quoted_and_backticked_phrases_are_mentions():
    out = challenge(
        "agent_report", 'The reviewer wrote "not now" and `follow-up` in the old report.', {}
    )
    assert out == {"verdicts": [], "flag_em": False}


def test_headings_and_clean_text_yield_nothing():
    out = challenge("plan", "## Open questions\n\nThe change landed and tests pass.", {})
    assert out == {"verdicts": [], "flag_em": False}


def test_deferrals_list_items_are_spans_in_order():
    out = challenge(
        "agent_report",
        "",
        {"deferrals": ["later", {"reason": "another repo: klabauter"}, ""]},
    )
    assert [(v["span"].split(":")[0], v["verdict"]) for v in out["verdicts"]] == [
        ("fields.deferrals[0]", "challenge"),
        ("fields.deferrals[1]", "pass"),
    ]


def test_unrecognised_unapplied_reason_challenges():
    _, v = _one("reviewer_unapplied", "", {"unapplied_reason": "I disagree with the reviewer"})
    assert v["verdict"] == "challenge" and v["reason_class"] is None


@pytest.mark.parametrize("text", [None, 7, ""])
@pytest.mark.parametrize("fields", [None, "x", [], {"deferrals": "later", "unapplied_reason": 3}])
def test_garbage_input_never_raises(text, fields):
    out = challenge("agent_report", text, fields)
    assert out == {"verdicts": [], "flag_em": False}


def test_append_flag_writes_one_json_line_and_creates_dir(tmp_path):
    rec = {"surface": "agent_report", "verdicts": [{"verdict": "challenge"}]}
    ref = append_flag(tmp_path, "sess-1", rec)
    assert ref["line"] == 1
    p = queue_path(tmp_path, "sess-1")
    assert ref["path"] == str(p)
    assert p.parent.name == "em-flags" and p.parent.parent.name == ".coordinator-local"
    assert [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()] == [rec]
    assert append_flag(tmp_path, "sess-1", {"n": 2})["line"] == 2
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2


def test_append_flag_unattributed_and_unsafe_session_ids(tmp_path):
    assert queue_path(tmp_path, None).name == "_unattributed.jsonl"
    assert queue_path(tmp_path, "").name == "_unattributed.jsonl"
    p = queue_path(tmp_path, "../../evil/x")
    assert p.parent == tmp_path / ".coordinator-local" / "em-flags"
    append_flag(tmp_path, "../../evil/x", {"a": "multi\nline"})
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1
