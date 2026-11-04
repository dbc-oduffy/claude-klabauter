"""plan.readiness_verdict records a verdict per declared anchor in the reply: engine-check fills
only the no-verdict gap, an explicit row outranks it, and every reply validates against the schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import _anchor_check
from coordinator_core.ops import plan_readiness_verdict as op

REL = "docs/plans/2026-10-15-fixture.md"
SCHEMA = Path(op.__file__).resolve().parents[1] / "contract" / "readiness-verdict.schema.json"
ANCHOR_ID = "fixture-fact"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _plan(root: Path) -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "coordinator_core").mkdir()
    (root / ".git").mkdir()
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-15\n"
        "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
        "capabilities: []\n"
        f"anchor_premises:\n  - id: {ANCHOR_ID}\n    claim: the fixture holds one marker\n"
        f"    check: {{command: 'grep -c marker {REL}', expect: '1'}}\n"
        "prime_exit_criterion:\n  statement: the op reports per class\n"
        "  derived_from: state/sizings/2026-10-15-fixture.yaml\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


def _validate(reply: dict) -> None:
    import jsonschema

    jsonschema.validate(reply, json.loads(SCHEMA.read_text(encoding="utf-8")))


def _evaluate(state: str, observed: str = "1", detail: str = "refused"):
    return lambda command, expect, repo_root: {"state": state, "observed": observed, "detail": detail}


def _entry(reply: dict) -> dict:
    (entry,) = reply["anchor_verdicts"]
    assert entry["id"] == ANCHOR_ID
    return entry


def test_match_with_no_rows_reads_ready_and_records_engine_check(tmp_path, monkeypatch):
    common = _plan(tmp_path)
    monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MATCH"))
    reply = op._handler({"plan": REL}, common)
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0
    assert _entry(reply)["source"] == "engine-check" and _entry(reply)["verdict"] == "RESOLVES"
    assert "1 anchor premise(s) resolved by in-process check" in reply["reason"]
    _validate(reply)


def test_mismatch_pulls_as_contradicted(tmp_path, monkeypatch):
    common = _plan(tmp_path)
    monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MISMATCH", observed="0"))
    reply = op._handler({"plan": REL}, common)
    assert reply["verdict"] == "judgment" and reply["blocking_findings"] == 1
    assert any(b.startswith(f"anchor-premise-CONTRADICTED: {ANCHOR_ID}") for b in reply["blocking"])
    assert _entry(reply)["verdict"] == "CONTRADICTED"
    _validate(reply)


def test_pipe_check_with_clean_checker_is_ready_and_noted_unevaluable(tmp_path):
    common = _plan(tmp_path)
    plan = tmp_path / REL
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            f"grep -c marker {REL}", "ls docs/plans | wc -l"
        ),
        encoding="utf-8",
    )
    reply = op._handler({"plan": REL, "premise": {"rows": []}}, common)
    assert reply["verdict"] == "ready" and reply["blocking_findings"] == 0
    assert not any("no-verdict" in b for b in reply["blocking"])
    assert _entry(reply)["source"] == "none" and _entry(reply)["verdict"] is None
    assert f"{ANCHOR_ID} (" in reply["reason"] and "not blocking" in reply["reason"]
    _validate(reply)


def test_pipe_check_with_a_contradicted_checker_row_still_blocks(tmp_path):
    common = _plan(tmp_path)
    plan = tmp_path / REL
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            f"grep -c marker {REL}", "ls docs/plans | wc -l"
        ),
        encoding="utf-8",
    )
    row = {"verdict": "CONTRADICTED", "citation": "the fixture marker", "anchor": ANCHOR_ID}
    reply = op._handler({"plan": REL, "premise": {"rows": [row]}}, common)
    assert reply["verdict"] == "judgment"
    assert any(b.startswith(f"anchor-premise-CONTRADICTED: {ANCHOR_ID}") for b in reply["blocking"])


def test_passed_unresolved_row_outranks_a_match(tmp_path, monkeypatch):
    common = _plan(tmp_path)
    monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MATCH"))
    row = {"verdict": "UNRESOLVED", "citation": "the fixture marker", "anchor": ANCHOR_ID}
    reply = op._handler({"plan": REL, "premise": {"rows": [row]}}, common)
    assert reply["verdict"] == "judgment"
    assert _entry(reply)["source"] == "row" and _entry(reply)["verdict"] == "UNRESOLVED"
    assert any(b.startswith(f"anchor-premise-UNRESOLVED: {ANCHOR_ID}") for b in reply["blocking"])
    _validate(reply)


def _anchor_ids(texts: list[str]) -> set[str]:
    return {t.split(": ", 1)[1].split(" ", 1)[0] for t in texts if t.startswith("anchor-premise-")}


def test_reason_blocking_and_rulings_name_the_same_declared_anchor_ids(tmp_path, monkeypatch):
    common = _plan(tmp_path)
    plan = tmp_path / REL
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            "anchor_premises:\n",
            "anchor_premises:\n  - id: P2\n    claim: second\n"
            f"    check: {{command: 'grep -c marker {REL}', expect: '1'}}\n",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MISMATCH", observed="0"))
    rows = [{"verdict": "UNRESOLVED", "citation": f"claim {i} " + "y" * 190} for i in range(8)]
    reply = op._handler({"plan": REL, "premise": {"rows": rows}}, common)
    declared = {"P2", ANCHOR_ID}
    blocking = _anchor_ids(reply["blocking"])
    assert blocking == declared
    assert _anchor_ids(reply["reason"].split("; ")) == declared
    assert _anchor_ids([r["subject"] for r in reply["rulings"]]) == declared
    assert len(reply["reason"]) <= 1000
    _validate(reply)


def test_lone_p2_two_e_anchor_is_evaluable_and_resolved_with_no_p1(tmp_path):
    common = _plan(tmp_path)
    target = tmp_path / "coordinator" / "contract" / "review-roster-fragment.json"
    target.parent.mkdir(parents=True)
    target.write_text('{\n"schema_version": 5,\n"kind": "review-wave"\n}\n', encoding="utf-8")
    plan = tmp_path / REL
    text = plan.read_text(encoding="utf-8")
    start = text.index("anchor_premises:")
    end = text.index("prime_exit_criterion:")
    anchor = (
        "anchor_premises:\n  - id: P2\n    claim: roster is v5 with one review-wave stage\n"
        "    check:\n      command: \"grep -c -e '\\\"schema_version\\\": 5' "
        "-e '\\\"kind\\\": \\\"review-wave\\\"' coordinator/contract/review-roster-fragment.json\"\n"
        "      expect: \"2\"\n"
    )
    plan.write_text(text[:start] + anchor + text[end:], encoding="utf-8")
    reply = op._handler({"plan": REL}, common)
    (entry,) = reply["anchor_verdicts"]
    assert entry["id"] == "P2" and entry["source"] == "engine-check"
    assert entry["verdict"] == "RESOLVES"
    assert not any("P1" in b or "not evaluable" in b for b in reply["blocking"])
    _validate(reply)


def test_plan_without_anchors_omits_the_key(tmp_path):
    common = _plan(tmp_path)
    plan = tmp_path / REL
    text = plan.read_text(encoding="utf-8")
    start = text.index("anchor_premises:")
    end = text.index("prime_exit_criterion:")
    plan.write_text(text[:start] + "anchor_premises: []\n" + text[end:], encoding="utf-8")
    reply = op._handler({"plan": REL}, common)
    assert "anchor_verdicts" not in reply
    _validate(reply)


def test_schema_refuses_an_unknown_source():
    import jsonschema

    bad = {"id": "x", "verdict": "RESOLVES", "source": "magic", "observed": ""}
    reply = op.readiness_verdict(
        {"plan": REL, "verdict": "PREPPED", "withheld_rows": [], "classes": {}, "message": ""}, None, []
    )
    reply = {"plan": REL, **reply, "prep_gate": {"plan": REL, "verdict": "PREPPED", "withheld_rows": [],
                                                  "classes": {}, "message": ""}, "anchor_verdicts": [bad]}
    with pytest.raises(jsonschema.ValidationError, match="magic"):
        _validate(reply)


def _carry_uncheckable(root: Path, plan: Path) -> None:
    from coordinator_core.ops._research_grounding import line_hashes
    from coordinator_core.session.machinery_paths import plan_sidecars_dir

    lines = plan.read_text(encoding="utf-8").splitlines()
    live = line_hashes("\n".join(lines))[next(i for i, ln in enumerate(lines) if "check:" in ln)]
    path = Path(plan_sidecars_dir(str(root))) / f"{plan.stem}.premise-carry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "schema": "premise-carry/1", "plan": REL, "brief_version": "v", "basis": [],
            "rows": [{"verdict": "UNCHECKABLE", "citation": "the fixture marker",
                      "anchor": ANCHOR_ID, "line_hash": live}],
        }),
        encoding="utf-8",
    )


def _home_check_plan(root: Path) -> Path:
    common = _plan(root)
    plan = root / REL
    plan.write_text(
        plan.read_text(encoding="utf-8").replace(
            f"grep -c marker {REL}", "grep -c crossSessionInbound ~/.claude/settings.json"
        ),
        encoding="utf-8",
    )
    return common


@pytest.mark.parametrize("lane", ["passed-row", "carry-row"])
@pytest.mark.parametrize("engine", ["mismatch", "real"])
def test_uncheckable_row_is_carried_never_relabelled_contradicted(tmp_path, monkeypatch, lane, engine):
    common = _home_check_plan(tmp_path)
    if engine == "mismatch":
        monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MISMATCH", observed="0"))
    else:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    params: dict = {"plan": REL}
    if lane == "passed-row":
        row = {"verdict": "UNCHECKABLE", "citation": "the fixture marker", "anchor": ANCHOR_ID}
        params["premise"] = {"rows": [row]}
    else:
        _carry_uncheckable(tmp_path, tmp_path / REL)
    reply = op._handler(params, common)
    assert not any("anchor-premise-CONTRADICTED" in b for b in reply["blocking"])
    assert _entry(reply)["verdict"] == "UNCHECKABLE"
    assert any(r["kind"] == "uncheckable-to-falsifier-row" for r in reply.get("mechanical_repairs", []))
    _validate(reply)


_AP1_CITATION = "AP1 -- crossSessionInbound is a top-level key of ~/.claude/settings.json"


def test_uncheckable_row_citing_the_anchor_id_first_without_an_anchor_key_is_carried(tmp_path, monkeypatch):
    common = _home_check_plan(tmp_path)
    monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MISMATCH", observed="0"))
    row = {"verdict": "UNCHECKABLE", "citation": _AP1_CITATION.replace("AP1", ANCHOR_ID, 1)}
    reply = op._handler({"plan": REL, "premise": {"rows": [row]}}, common)
    assert not any("anchor-premise-CONTRADICTED" in b for b in reply["blocking"])
    assert _entry(reply)["verdict"] == "UNCHECKABLE" and _entry(reply)["source"] == "row"
    assert any(r["kind"] == "uncheckable-to-falsifier-row" for r in reply["mechanical_repairs"])
    _validate(reply)


def test_uncheckable_row_mentioning_the_anchor_id_in_passing_does_not_clear_a_broke_check(tmp_path, monkeypatch):
    common = _home_check_plan(tmp_path)
    monkeypatch.setattr(_anchor_check, "evaluate", _evaluate("MISMATCH", observed="0"))
    row = {"verdict": "UNCHECKABLE", "citation": f"a different claim that cites {ANCHOR_ID} in passing"}
    reply = op._handler({"plan": REL, "premise": {"rows": [row]}}, common)
    assert any(b.startswith(f"anchor-premise-CONTRADICTED: {ANCHOR_ID}") for b in reply["blocking"])
