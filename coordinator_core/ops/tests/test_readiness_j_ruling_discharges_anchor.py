"""plan.readiness_verdict: a live J-ruling revise note naming an anchor discharges its blocking finding."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import plan_readiness_verdict as op
from coordinator_core.ops.tests.test_readiness_own_run_withheld_premise import REL, RUN, SIZING, _repo

ANCHOR = "AP3"


def _anchored_repo(root: Path) -> Path:
    common = _repo(root)
    plan = root / REL
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "anchor_premises: []\n",
            f"anchor_premises:\n  - id: {ANCHOR}\n    claim: the fixture holds one marker\n"
            f"    check: {{command: 'grep -c marker {REL}', expect: '1'}}\n"
            f"  - id: AP4\n    claim: the fixture holds another marker\n"
            f"    check: {{command: 'grep -c marker {REL}', expect: '1'}}\n",
        ),
        encoding="utf-8",
    )
    return common


def _run(root: Path, notes: list) -> None:
    record = root / "state" / "warp" / RUN
    record.mkdir(parents=True, exist_ok=True)
    (record / "halted.json").write_text(json.dumps({"status": "halted", "sizing_rel": SIZING}), encoding="utf-8")
    (record / "notes.jsonl").write_text(
        "".join(json.dumps(n) + "\n" for n in notes), encoding="utf-8"
    )


def _reply(common: Path) -> dict:
    rows = [
        {"verdict": "CONTRADICTED", "citation": "the fixture marker", "anchor": ANCHOR},
        {"verdict": "RESOLVES", "citation": "the other marker", "anchor": "AP4"},
    ]
    return op._handler({"plan": REL, "premise": {"rows": rows}}, common)


def test_pulled_without_a_ruling(tmp_path):
    common = _anchored_repo(tmp_path)
    reply = _reply(common)
    assert reply["verdict"] != "ready" and reply["blocking_findings"] == 1


def test_j1_note_naming_the_anchor_is_ready_on_refire(tmp_path):
    common = _anchored_repo(tmp_path)
    _run(tmp_path, [{"note": "J1: AP3 satisfied by the run's own hunk", "at": "t"}])
    reply = _reply(common)
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0
    assert "AP3 discharged by EM ruling J1" in reply["reason"]


def test_note_naming_another_anchor_still_pulls(tmp_path):
    common = _anchored_repo(tmp_path)
    _run(tmp_path, [{"note": "J1: AP4 satisfied", "at": "t"}])
    assert _reply(common)["blocking_findings"] == 1


def test_note_without_a_j_id_still_pulls(tmp_path):
    common = _anchored_repo(tmp_path)
    _run(tmp_path, [{"note": "AP3 is satisfied, trust me", "at": "t"}])
    assert _reply(common)["blocking_findings"] == 1


def test_retracted_j_note_still_pulls(tmp_path):
    common = _anchored_repo(tmp_path)
    _run(tmp_path, [{"note": "J1: AP3 satisfied", "at": "t"}, {"retracts": "N1", "reason": "wrong", "at": "t"}])
    assert _reply(common)["blocking_findings"] == 1


def test_anchor_id_prefix_is_not_a_name(tmp_path):
    common = _anchored_repo(tmp_path)
    _run(tmp_path, [{"note": "J1: AP31 satisfied", "at": "t"}])
    assert _reply(common)["blocking_findings"] == 1


def test_one_j_note_after_a_plain_note_discharges_both_named_anchors(tmp_path):
    common = _anchored_repo(tmp_path)
    _run(tmp_path, [{"note": "plain context note", "at": "t"}, {"note": "J2: AP3 and AP4 satisfied", "at": "t"}])
    rows = [
        {"verdict": "CONTRADICTED", "citation": "the fixture marker", "anchor": ANCHOR},
        {"verdict": "CONTRADICTED", "citation": "the other marker", "anchor": "AP4"},
    ]
    reply = op._handler({"plan": REL, "premise": {"rows": rows}}, common)
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0
    assert "AP3 discharged by EM ruling J2" in reply["reason"] and "AP4 discharged by EM ruling J2" in reply["reason"]
