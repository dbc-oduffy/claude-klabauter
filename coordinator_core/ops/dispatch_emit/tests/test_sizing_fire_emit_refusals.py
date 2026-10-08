"""Scaffold-residue refusals and the accept-launcher hint in collect_fire_refusals."""

from __future__ import annotations

from pathlib import PurePath

from coordinator_core.ops.dispatch_emit import sizing_fire as sf
from coordinator_core.session.record_homes import record_path

REL = PurePath(record_path("", "sizings", "a.yaml")).as_posix()


def _fireable(**over):
    doc = {
        "intent": "Fix the friction",
        "estimate": {"tshirt": "M"},
        "route": "plan",
        "status": "sized",
        "interaction_mode": "pm",
        "premise": {"provenance": "executed", "evidence": "ran the gate, saw the halt"},
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes"}},
    }
    doc.update(over)
    return doc


def _refusals(doc, arm="m_plus"):
    return sf.collect_fire_refusals(doc, sizing_rel=REL, arm=arm, writes=[])


def test_fresh_scaffold_default_refused_in_one_list():
    doc = _fireable(
        intent="PLACEHOLDER — replace with the PM's ask, verbatim",
        status="draft",
        estimate={"tshirt": "XS"},
        route="dispatch",
        premise={
            "provenance": "unrecorded",
            "evidence": "PLACEHOLDER — cite the file:line, test, or command output you actually looked at",
        },
    )
    joined = " | ".join(_refusals(doc, arm="m_plus"))
    for needle in ("`status`", "`intent`", "`premise.evidence`", "`premise.provenance`"):
        assert needle in joined


def test_deliberate_sizing_yields_no_refusals():
    assert _refusals(_fireable()) == []


def test_each_residue_field_refused_alone():
    assert len(_refusals(_fireable(intent="PLACEHOLDER"))) == 1
    assert len(_refusals(_fireable(premise={"provenance": "read", "evidence": "PLACEHOLDER x"}))) == 1
    assert len(_refusals(_fireable(premise={"provenance": "unrecorded", "evidence": "real"}))) == 1


def test_word_inside_intent_is_not_residue():
    assert _refusals(_fireable(intent="Replace the PLACEHOLDER guard")) == []


def test_accept_hint_names_launcher_and_keeps_prefix():
    out = _refusals(_fireable(exit_criterion={"statement": "done", "accepted": None}))
    assert len(out) == 1
    assert out[0].startswith("`exit_criterion.accepted` is null")
    assert f"sizing-accept-exit-criterion --sizing {REL} --pm-quote" in out[0]
