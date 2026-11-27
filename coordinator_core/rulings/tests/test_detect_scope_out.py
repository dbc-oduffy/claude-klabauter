"""A line that scopes an accepted ruling out of the run is neither a deferral nor a contradiction;
a real deferral or contradiction of the same ruling still refuses (through prep_gate's RULINGS)."""
from __future__ import annotations

import pytest

from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling
from coordinator_core.rulings.detect import detect

RID = "DR-pm-narrow-slice-then-runpod"
R = Ruling(
    id=RID,
    pm_turn="docs/decisions/DR-pm-narrow-slice-then-runpod.md",
    scope=("metahuman", "runpod"),
    text="MetaHuman work proceeds as a narrow slice first, then RunPod",
    recorded="2026-11-01",
)

SCOPE_OUTS = [
    f"N2: The MetaHuman ruling {RID} is not in scope for this run.",
    f"- N2: Do not touch the MetaHuman work ({RID}); out of scope here.",
    f"N2: {RID} is out of scope here; leave it to the PM.",
    f"Out of scope: MetaHuman narrow-slice work ({RID}) is excluded from this plan.",
]


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    store.append(tmp_path, R)
    return tmp_path


def _prep(text, root):
    from coordinator_core.roadmap import prep_gate

    return prep_gate._rulings(text, root)


@pytest.mark.parametrize("line", SCOPE_OUTS)
def test_scope_out_passes_prep_rulings_gate(root, line):
    out = _prep(f"# Plan\n\nRun the inbox archive.\n\n## Requirements ledger\n\n{line}\n", root)
    assert out["status"] == "PASS", out


@pytest.mark.parametrize("line", SCOPE_OUTS)
def test_scope_out_passes_detect(tmp_path, line):
    assert detect(line, [R], tmp_path)["verdict"] == "pass"


APPLIED = [
    f"Applying {RID}: never as a source comment, and no gate message calls these bars rule 4.",
    f"Per {RID}, the slice is not optional and no RunPod work starts first.",
    f"Per an APM ruling ({RID}) the MetaHuman bar is not a source comment.",
    f"Under APM ruling {RID}: never skip the narrow slice.",
]
NEGATED_APPLICATION = [
    f"We are not applying {RID} here.",
    f"We do not follow {RID}.",
    f"Per {RID} we would stop, but we override it.",
    f"No longer per {RID}.",
]


@pytest.mark.parametrize("line", APPLIED)
def test_applied_citation_with_negation_in_content_passes(root, tmp_path, line):
    assert detect(line, [R], tmp_path)["verdict"] == "pass"
    assert _prep(f"# Plan\n\n{line}\n", root)["status"] == "PASS"


@pytest.mark.parametrize("line", NEGATED_APPLICATION)
def test_negated_application_still_flags(root, tmp_path, line):
    out = detect(line, [R], tmp_path)
    assert out["verdict"] == "refuse"
    assert "cited_with_negation" in out["reasons"][RID]
    assert _prep(f"# Plan\n\n{line}\n", root)["status"] != "PASS"


@pytest.mark.parametrize(
    "text, key, reason",
    [
        (f"We will not follow {RID} in this plan.", "contradicted", "cited_with_negation"),
        (f"{RID} is out of scope, but we override it for the RunPod step.", "contradicted", "cited_with_negation"),
        (f"{RID} is out of scope. We will not follow {RID} anyway.", "contradicted", "cited_with_negation"),
        ("The MetaHuman RunPod choice is pending a PM ruling on slice size?", "deferred", None),
        (f"Out of scope: {RID}.\n\nThe MetaHuman slice is pending a PM ruling on size?", "deferred", None),
        (f"Out of scope: {RID}.\nThe MetaHuman slice is pending a PM ruling on size?", "deferred", None),
        (f"contradicts: {RID}", "contradicted", "explicit_contradicts"),
    ],
)
def test_real_deferral_and_contradiction_still_refuse(root, tmp_path, text, key, reason):
    out = detect(text, [R], tmp_path)
    assert out["verdict"] == "refuse"
    assert out["route"] == "coordinator:apm"
    assert out[key] == [RID]
    if reason:
        assert reason in out["reasons"][RID]
    prep = _prep(text, root)
    assert prep["status"] != "PASS"
    assert "coordinator:apm" in prep["detail"]
