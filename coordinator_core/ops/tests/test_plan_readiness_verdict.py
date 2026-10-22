"""plan.readiness_verdict: reply shape against the pinned schema, verdict classes, no skip param,
zero spawns and the process-time bar on a representative plan; corrects-list cover, stale
baseline_ref rows and the `blocking` key.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_readiness_verdict as op

REL = "docs/plans/2026-10-15-fixture.md"
SCHEMA = Path(op.__file__).resolve().parents[1] / "contract" / "readiness-verdict.schema.json"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _plan(root: Path, statement: str = "the op reports per class") -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-15\n"
        "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
        f"prime_exit_criterion:\n  statement: {statement}\n"
        "  derived_from: state/sizings/2026-10-15-fixture.yaml\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


def _validate(reply: dict) -> None:
    import jsonschema

    jsonschema.validate(reply, json.loads(SCHEMA.read_text(encoding="utf-8")))


def _gate(*classes: dict, verdict: str = "NOT-PREPPED") -> dict:
    return {
        "plan": REL,
        "verdict": verdict,
        "withheld_rows": [],
        "classes": {f"C{i}": c for i, c in enumerate(classes)},
        "message": "",
    }


PASS = {"status": "PASS", "kind": None, "mechanical": False, "repair": None}
MECH = {"status": "DEFECT", "kind": "prime-exit-suite-tier", "mechanical": True, "repair": "edit it"}
JUDG = {"status": "DEFECT", "kind": "census-incomplete", "mechanical": False, "repair": None}


def test_prepped_plan_without_findings_is_ready():
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, [])
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("ready", "none", [])


def test_mechanical_prep_failure_pulls_with_repairs():
    out = op.readiness_verdict(_gate(PASS, MECH), None, [])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    assert out["mechanical_repairs"] == [
        {"kind": "prime-exit-suite-tier", "target": "C1", "instruction": "edit it"}
    ]


def test_no_writes_executor_heavy_gate_result_reads_mechanical(tmp_path):
    from coordinator_core.roadmap import prep_gate as pg
    from coordinator_core.roadmap.tests.test_prep_gate_four_legs import _CLEAN_FM, _write_plan

    spine = (
        "- id: C1\n  title: Baseline\n  change_kind: code-edit\n  surface: src/a.py\n"
        "  writes: []\n  disposition: open\n  body: |\n    Measure with `pnpm vitest run`.\n"
    )
    cls = pg.gate_plan(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))["classes"]["EXECUTOR_HEAVY"]
    out = op.readiness_verdict(_gate(cls), None, [])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    assert out["mechanical_repairs"][0]["kind"] == "executor-heavy-command-row"


def test_non_mechanical_prep_failure_is_substantive_and_drops_repairs():
    out = op.readiness_verdict(_gate(MECH, JUDG), None, [])
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("pulled", "substantive", [])


def test_coverage_mechanical_findings_become_repairs():
    cov = {"verdict": "pull", "mechanicalFindings": ["add dlv id"], "judgmentFindings": []}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert (out["verdict"], out["pull_class"]) == ("pulled", "mechanical")
    assert "add dlv id" in out["mechanical_repairs"][0]["instruction"]


def test_coverage_counts_are_accepted():
    cov = {"verdict": "pull", "mechanicalFindings": 2, "judgmentFindings": 0}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert len(out["mechanical_repairs"]) == 2


def test_judgment_finding_needs_an_agent():
    cov = {"verdict": "pull", "mechanicalFindings": [], "judgmentFindings": ["scope unclear"]}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert (out["verdict"], out["pull_class"], out["mechanical_repairs"]) == ("judgment", "none", [])


def test_blocking_or_unknown_reviewer_verdict_fails_closed_to_judgment():
    for verdict in ("reject", "something-new"):
        out = op.readiness_verdict(
            _gate(PASS, verdict="PREPPED"), None, [{"reviewer": "patrik", "verdict": verdict}]
        )
        assert out["verdict"] == "judgment"


def test_approving_reviewers_keep_a_clean_plan_ready():
    out = op.readiness_verdict(
        _gate(PASS, verdict="PREPPED"),
        {"verdict": "approved", "mechanicalFindings": [], "judgmentFindings": []},
        [{"reviewer": "patrik", "verdict": "approved"}],
    )
    assert out["verdict"] == "ready"


def test_refused_prep_gate_is_substantive():
    out = op.readiness_verdict(_gate(PASS, verdict="REFUSED"), None, [])
    assert out["pull_class"] == "substantive"


@pytest.mark.parametrize("bad", [{"force": True}, {"skip": True}, {"skip_prep": True}])
def test_no_skip_or_force_param_is_accepted(tmp_path, bad):
    common = _plan(tmp_path)
    with pytest.raises(ValueError, match="unknown param"):
        op._handler({"plan": REL, **bad}, common)


def test_coverage_must_be_an_object(tmp_path):
    common = _plan(tmp_path)
    with pytest.raises(ValueError, match="coverage"):
        op._handler({"plan": REL, "coverage": "pull"}, common)


def test_zero_dispatchable_rows_fail_spine_and_name_the_em_owned_rows(tmp_path):
    common = _plan(tmp_path)
    plan = tmp_path / REL
    plan.write_text(
        plan.read_text(encoding="utf-8").replace("  disposition: open\n", "  disposition: open\n  performer: em\n"),
        encoding="utf-8",
    )
    reply = op._handler({"plan": REL}, common)
    spine = reply["prep_gate"]["classes"]["SPINE"]
    assert spine["status"] == "DEFECT" and spine["kind"] == "zero-dispatchable-rows"
    assert "owed by the EM: C1" in spine["detail"]
    assert reply["verdict"] == "pulled" and reply["pull_class"] == "substantive"
    assert "owed by the EM: C1" in reply["reason"]
    _validate(reply)


def test_handler_reply_validates_and_spawns_nothing_under_the_bar(tmp_path, monkeypatch):
    common = _plan(tmp_path)
    with _count_spawns_attributed(monkeypatch) as spawns:
        start = time.process_time()
        reply = op._handler({"plan": REL}, common)
        elapsed_ms = (time.process_time() - start) * 1000
    assert spawns == [], [s.argv for s in spawns]
    assert elapsed_ms < 500, elapsed_ms
    assert reply["plan"] == REL
    assert reply["prep_gate"]["plan"] == REL
    _validate(reply)


def _ledger(root: Path, rows: list, *, stamped: bool = True, name: str = "rev.md") -> str:
    stamp = "findings_ledger: {rows: 1}\n" if stamped else ""
    (root / name).write_text(
        f"---\nagent_type: coordinator:code-reviewer\n{stamp}---\n# R\n\n## Findings Ledger\n\n"
        f"```json\n{json.dumps(rows)}\n```\n",
        encoding="utf-8",
    )
    return name


def _review(verdict: str, ledger=None, **extra) -> list:
    entry = {"reviewer": "patrik", "verdict": verdict, **extra}
    if ledger is not None:
        entry["ledger"] = ledger
    return [entry]


def _verdict_for(root: Path, reviews: list) -> str:
    return op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, reviews, root)["verdict"]


def test_complete_coverage_without_findings_is_ready():
    cov = {"verdict": "COMPLETE", "mechanicalFindings": [], "judgmentFindings": []}
    assert op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])["verdict"] == "ready"


@pytest.mark.parametrize("word", ["INCOMPLETE", "SCOPE-MISMATCH"])
def test_non_clear_coverage_without_findings_never_pulls(word):
    cov = {"verdict": word, "mechanicalFindings": [], "judgmentFindings": []}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert (out["verdict"], out["pull_class"]) == ("ready", "none")
    assert "carries no findings" in out["reason"]


def _rows(*verdicts, citation="c"):
    return [{"verdict": v, "citation": f"{citation}-{i}"} for i, v in enumerate(verdicts)]


def _premise(rows, reviews=None):
    return op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, reviews or [], None, rows)


def test_premise_uncheckable_only_ledger_is_ready_with_note():
    rows = _rows("UNCHECKABLE", "UNCHECKABLE", "UNCHECKABLE")
    rows[0]["questionClass"] = 5
    rows[2]["questionClass"] = 4
    out = _premise(rows)
    assert (out["verdict"], out["pull_class"]) == ("ready", "none")
    assert "3 UNCHECKABLE premise row(s) left to reviewer judgment" in out["reason"]
    _validate({"plan": REL, **out, "prep_gate": _gate(PASS, verdict="PREPPED")})


def test_premise_uncheckable_citing_a_ruling_never_pulls_on_repeat_passes():
    rows = [{"verdict": "UNCHECKABLE", "citation": "Settled premises: ruling X", "evidence": "state/apm-rulings/x.md"}]
    outs = [_premise(rows) for _ in range(3)]
    assert [(o["verdict"], o["pull_class"]) for o in outs] == [("ready", "none")] * 3


def test_premise_blocking_citation_is_capped_at_200_characters():
    out = _premise([{"verdict": "UNRESOLVED", "citation": "y" * 300}])
    assert "y" * 200 in out["reason"]
    assert "y" * 201 not in out["reason"]


def test_premise_uncheckable_plus_resolves_is_ready():
    assert _premise(_rows("UNCHECKABLE", "RESOLVES"))["verdict"] == "ready"


@pytest.mark.parametrize("word", ["UNRESOLVED", "CONTRADICTED"])
def test_premise_unresolved_or_contradicted_row_blocks_and_names_citation(word):
    out = _premise(_rows("UNCHECKABLE", word, citation="wiki/path.md"))
    assert (out["verdict"], out["pull_class"]) == ("judgment", "none")
    assert f"premise-check: {word} wiki/path.md-1" in out["reason"]
    _validate({"plan": REL, **out, "prep_gate": _gate(PASS, verdict="PREPPED")})


def test_premise_unknown_row_verdict_is_refused(tmp_path):
    common = _plan(tmp_path)
    with pytest.raises(ValueError, match=r"premise\.rows\[0\]"):
        op._handler({"plan": REL, "premise": {"rows": _rows("MAYBE")}}, common)


def test_premise_with_premise_check_review_drops_the_review_entry(tmp_path):
    common = _plan(tmp_path)
    reviews = [{"reviewer": "premise-check", "verdict": "BLOCKED"}]
    reply = op._handler({"plan": REL, "premise": {"rows": _rows("RESOLVES")}, "reviews": reviews}, common)
    assert "premise-check" not in reply["reason"]


def test_c5bf35_inputs_return_a_usable_reply_not_an_error(tmp_path):
    """Run c5bf35 passed premise-check=OK among reviews plus UNCHECKABLE premise rows."""
    common = _plan(tmp_path)
    reviews = [
        {"reviewer": "premise-check", "verdict": "OK"},
        {"reviewer": "coordinator:staff-eng", "verdict": "WARN"},
        {"reviewer": "coordinator:apm", "verdict": "WARN"},
    ]
    reply = op._handler(
        {"plan": REL, "premise": {"rows": _rows("UNCHECKABLE", "UNCHECKABLE")}, "reviews": reviews}, common
    )
    assert reply["verdict"] == "judgment"
    assert "premise-check" not in reply["reason"]


def test_premise_reaches_the_op_through_the_handler(tmp_path):
    common = _plan(tmp_path)
    reply = op._handler({"plan": REL, "premise": {"rows": _rows("CONTRADICTED", citation="zz")}}, common)
    assert "premise-check: CONTRADICTED zz-0" in reply["reason"]
    _validate(reply)


def test_premise_empty_rows_behave_as_absent(tmp_path):
    common = _plan(tmp_path)
    a = op._handler({"plan": REL}, common)
    b = op._handler({"plan": REL, "premise": {"rows": []}}, common)
    assert a == b


def test_premise_overflowing_citations_end_with_more_tail():
    out = _premise(_rows(*["UNRESOLVED"] * 10, citation="x" * 190))
    assert out["verdict"] == "judgment"
    assert out["reason"].endswith("more premise row(s)")
    assert len(out["reason"]) <= 1000


@pytest.mark.parametrize("verdict", ["WARN", "BLOCKED"])
def test_verified_ledger_makes_warn_and_blocked_ready(tmp_path, verdict):
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True}])
    assert _verdict_for(tmp_path, _review(verdict, ledger)) == "ready"


def test_warn_without_ledger_unstamped_or_unverified_stays_judgment(tmp_path):
    assert _verdict_for(tmp_path, _review("WARN")) == "judgment"
    assert _verdict_for(tmp_path, _review("WARN", "missing.md")) == "judgment"
    unstamped = _ledger(tmp_path, [{"id": "F1", "verified": True}], stamped=False, name="u.md")
    assert _verdict_for(tmp_path, _review("WARN", unstamped)) == "judgment"
    mixed = _ledger(
        tmp_path, [{"id": "F1", "verified": True}, {"id": "F2"}], name="m.md"
    )
    assert _verdict_for(tmp_path, _review("WARN", mixed)) == "judgment"


def test_stamped_ledger_with_no_rows_leaves_review_at_judgment(tmp_path):
    ledger = _ledger(tmp_path, [])
    assert _verdict_for(tmp_path, _review("BLOCKED", ledger)) == "judgment"


@pytest.mark.parametrize("status", ["em-rejected", "suspended"])
def test_non_applied_row_leaves_review_at_judgment(tmp_path, status):
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True, "status": status}])
    assert _verdict_for(tmp_path, _review("BLOCKED", ledger)) == "judgment"


def test_applied_status_row_is_clear(tmp_path):
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True, "status": "applied"}])
    assert _verdict_for(tmp_path, _review("WARN", ledger)) == "ready"


@pytest.mark.parametrize("verdict", ["PIVOT", "REJECTED", "FAILED", "mystery"])
def test_ledger_never_clears_other_verdicts(tmp_path, verdict):
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True}])
    assert _verdict_for(tmp_path, _review(verdict, ledger)) == "judgment"


def test_unknown_key_in_a_review_entry_changes_nothing(tmp_path):
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True}])
    assert _verdict_for(tmp_path, _review("WARN", ledger, extra_key="x")) == "ready"
    assert _verdict_for(tmp_path, _review("WARN", extra_key="x")) == "judgment"


def test_handler_reads_the_ledger_and_never_writes_it(tmp_path):
    common = _plan(tmp_path)
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True}])
    absolute = str(tmp_path / ledger)
    before = (tmp_path / ledger).read_bytes()
    reply = op._handler({"plan": REL, "reviews": _review("WARN", absolute)}, common)
    assert (tmp_path / ledger).read_bytes() == before
    assert "needs an agent" not in reply["reason"]


def test_handler_reply_for_a_suite_tier_statement_validates(tmp_path):
    common = _plan(tmp_path, statement="the fast tier is green")
    reply = op._handler({"plan": REL}, common)
    _validate(reply)
    assert reply["verdict"] in {"pulled", "judgment", "ready"}


def _corrects_plan(root: Path, corrects: str, fixer_disposition: str = "open", ref: str = "aaa111") -> Path:
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "coordinator_core" / "ops").mkdir(parents=True)
    spine = _SPINE + _SPINE.replace("id: C1", "id: C2").replace(
        "disposition: open", f"disposition: {fixer_disposition}"
    )
    (root / REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-15\n"
        "census:\n  - question: q\n    command: grep -c a b\n    result: '1'\n"
        f"{corrects}"
        "prime_exit_criterion:\n  statement: the op reports per class\n"
        "  derived_from: state/sizings/2026-10-15-fixture.yaml\n"
        f"  falsifier:\n    how: run it\n    baseline_output: out\n    baseline_ref: {ref}\n"
        "    expected_when_true: ok\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + spine + "```\n",
        encoding="utf-8",
    )
    return root / ".git"


_CORRECTS = "corrects:\n  - citation: tree claim X\n    fixed_by: C2\n"


def _op_reply(root: Path, rows: list) -> dict:
    reply = op._handler({"plan": REL, "premise": {"rows": rows}}, root)
    _validate(reply)
    return reply


def _row(verdict="CONTRADICTED", citation="see tree claim X here", **extra) -> dict:
    return {"verdict": verdict, "citation": citation, **extra}


def test_contradicted_row_on_corrects_list_with_spine_fix_is_a_note(tmp_path):
    common = _corrects_plan(tmp_path, _CORRECTS)
    reply = _op_reply(common, [_row()])
    assert "premise-check: CONTRADICTED" not in reply["reason"]
    assert reply["blocking"] == []
    assert "tree claim X -> C2" in reply["reason"]


def test_plan_whose_only_blockers_are_corrected_reads_ready(tmp_path):
    corrects = _CORRECTS + "  - citation: tree claim Y\n    fixed_by: C1\n"
    common = _corrects_plan(tmp_path, corrects)
    rows = [_row(), _row(citation="tree claim Y"), _row("RESOLVES", citation="other")]
    reply = _op_reply(common, rows)
    assert reply["verdict"] == "ready"
    assert reply["blocking"] == []
    assert "2 CONTRADICTED premise row(s) the plan corrects" in reply["reason"]


def test_corrects_entry_fixed_by_commit_shas_covers_the_row(tmp_path):
    # Shape from a live halt: `fixed_by` names landed commits, as the premise checker cites them.
    corrects = 'corrects:\n  - citation: tree claim X\n    fixed_by: "6b07cc0fa, 8fdeaddba, 0044cec8b"\n'
    common = _corrects_plan(tmp_path, corrects)
    reply = _op_reply(common, [_row(citation="corrects: 'tree claim X'", evidence="on corrects: -> 0044cec8b")])
    assert reply["verdict"] == "ready"
    assert reply["blocking"] == []


_TWO = (
    "corrects:\n  - citation: tree claim X\n    fixed_by: C2\n"
    "  - citation: tree claim Y\n    fixed_by: C1\n"
)
_COMPOSITE = "tree claim X holds; tree claim Y is designed (...)"


def test_composite_citation_with_every_sentence_corrected_is_a_note(tmp_path):
    reply = _op_reply(_corrects_plan(tmp_path, _TWO), [_row(citation=_COMPOSITE)])
    assert reply["blocking"] == []
    assert "tree claim X + tree claim Y -> C2, C1" in reply["reason"]


def test_composite_citation_with_an_uncited_sentence_still_blocks(tmp_path):
    reply = _op_reply(_corrects_plan(tmp_path, _TWO), [_row(citation=_COMPOSITE + ". unrelated claim Z")])
    assert reply["blocking"] != []


def test_composite_cover_ignores_entries_without_a_live_fixer(tmp_path):
    corrects = _TWO.replace("fixed_by: C1", "fixed_by: C9")
    reply = _op_reply(_corrects_plan(tmp_path, corrects), [_row(citation=_COMPOSITE)])
    assert reply["blocking"] != []


@pytest.mark.parametrize(
    "corrects, disposition",
    [
        ("corrects:\n  - citation: tree claim X\n    fixed_by: C9\n", "open"),
        (_CORRECTS, "backlogged"),
        (_CORRECTS, "spun_off"),
    ],
)
def test_corrects_entry_without_a_live_fixing_row_still_blocks(tmp_path, corrects, disposition):
    common = _corrects_plan(tmp_path, corrects, disposition)
    reply = _op_reply(common, [_row()])
    assert reply["verdict"] == "judgment"
    assert reply["blocking"] == ["premise-check: CONTRADICTED see tree claim X here"]


# Plan schema 2.39.0 types `corrects`, so a malformed entry fails the prep gate before the
# premise rows are read; it must never reach the note path.
@pytest.mark.parametrize(
    "corrects", ["corrects:\n  - citation: tree claim X\n", "corrects: nonsense\n"]
)
def test_malformed_corrects_is_pulled_as_schema_invalid(tmp_path, corrects):
    reply = _op_reply(_corrects_plan(tmp_path, corrects), [_row()])
    assert reply["verdict"] == "pulled"
    assert "SCHEMA:schema-invalid" in reply["reason"]


def test_unresolved_row_on_corrects_list_still_blocks(tmp_path):
    common = _corrects_plan(tmp_path, _CORRECTS)
    reply = _op_reply(common, [_row("UNRESOLVED")])
    assert reply["verdict"] == "judgment"
    assert reply["blocking"] == ["premise-check: UNRESOLVED see tree claim X here"]


def test_corrects_citation_is_whitespace_normalised_and_case_sensitive(tmp_path):
    common = _corrects_plan(tmp_path, _CORRECTS)
    assert _op_reply(common, [_row(citation="tree   claim\nX")])["verdict"] == "ready"
    assert _op_reply(common, [_row(citation="TREE CLAIM X")])["verdict"] == "judgment"


def test_stale_class4_row_is_a_note_and_matching_baseline_ref_still_blocks(tmp_path):
    common = _corrects_plan(tmp_path, "", ref="new222")
    stale = _op_reply(common, [_row(citation="falsifier", baseline_ref="old111")])
    assert stale["verdict"] == "ready"
    assert "superseded baseline old111" in stale["reason"]
    assert stale["blocking"] == []
    fresh = _op_reply(common, [_row(citation="falsifier", baseline_ref="new222")])
    assert fresh["verdict"] == "judgment"
    assert fresh["blocking"] == ["premise-check: CONTRADICTED falsifier"]


def test_non_string_baseline_ref_is_refused(tmp_path):
    common = _corrects_plan(tmp_path, "")
    with pytest.raises(ValueError, match="baseline_ref"):
        op._handler({"plan": REL, "premise": {"rows": [_row(baseline_ref=3)]}}, common)


def test_blocking_is_sorted_deduplicated_and_empty_on_ready():
    rows = [_row("UNRESOLVED", "b"), _row("CONTRADICTED", "a"), _row("UNRESOLVED", "b")]
    out = _premise(rows, [{"reviewer": "patrik", "verdict": "reject"}])
    assert out["blocking"] == [
        "the Staff Engineer: 'reject'",
        "premise-check: CONTRADICTED a",
        "premise-check: UNRESOLVED b",
    ]
    assert _premise([])["blocking"] == []
    assert op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, [])["blocking"] == []


def _absent_plan(root: Path, tracked_by: str, open_questions: str = "") -> Path:
    block = f'declared_absent:\n  - citation: bin/verify-routing-table.sh\n    tracked_by: "{tracked_by}"\n'
    common = _corrects_plan(root, block)
    if open_questions:
        plan = root / REL
        plan.write_text(
            plan.read_text(encoding="utf-8") + f"\n## Open questions\n\n{open_questions}\n\n## Later\n",
            encoding="utf-8",
        )
    return common


def _absent_row(verdict="UNRESOLVED") -> dict:
    return _row(verdict, citation="`bin/verify-routing-table.sh` exists")


def test_declared_absent_unresolved_row_with_filed_ask_is_a_note(tmp_path):
    ask = tmp_path / "state" / "asks" / "routing-table.md"
    ask.parent.mkdir(parents=True)
    ask.write_text("ask\n", encoding="utf-8")
    common = _absent_plan(tmp_path, "state/asks/routing-table.md")
    reply = _op_reply(common, [_absent_row()])
    assert reply["verdict"] == "ready"
    assert reply["blocking"] == []
    assert (
        "1 UNRESOLVED premise row(s) the plan declares absent: "
        "bin/verify-routing-table.sh -> state/asks/routing-table.md"
    ) in reply["reason"]


def test_declared_absent_covers_through_an_open_question_or_an_https_url(tmp_path):
    common = _absent_plan(tmp_path, "routing table script not yet built", "- routing table script not yet built?")
    assert _op_reply(common, [_absent_row()])["verdict"] == "ready"
    url = tmp_path / "other"
    url.mkdir()
    common = _absent_plan(url, "https://example.invalid/issues/7")
    assert _op_reply(common, [_absent_row()])["blocking"] == []


def test_declared_absent_with_unresolvable_tracked_by_still_blocks(tmp_path):
    common = _absent_plan(tmp_path, "state/asks/never-filed.md", "- something else entirely")
    reply = _op_reply(common, [_absent_row()])
    assert reply["verdict"] == "judgment"
    assert reply["blocking"] == ["premise-check: UNRESOLVED `bin/verify-routing-table.sh` exists"]


def test_declared_absent_tracked_by_outside_the_repo_does_not_cover(tmp_path):
    (tmp_path / "outside.md").write_text("x", encoding="utf-8")
    root = tmp_path / "repo"
    assert _op_reply(_absent_plan(root, "../outside.md"), [_absent_row()])["verdict"] == "judgment"


def test_declared_absent_never_covers_a_contradicted_row(tmp_path):
    common = _absent_plan(tmp_path, "https://example.invalid/issues/7")
    reply = _op_reply(common, [_absent_row("CONTRADICTED")])
    assert reply["verdict"] == "judgment"
    assert reply["blocking"] == ["premise-check: CONTRADICTED `bin/verify-routing-table.sh` exists"]


def test_plan_whose_only_blocker_is_declared_absent_reads_ready_beside_other_rows(tmp_path):
    common = _absent_plan(tmp_path, "https://example.invalid/issues/7")
    reply = _op_reply(common, [_absent_row(), _row("RESOLVES", citation="other")])
    assert reply["verdict"] == "ready"
    assert reply["blocking"] == []


def test_blocking_is_not_truncated_by_the_reason_tail():
    out = _premise(_rows(*["UNRESOLVED"] * 10, citation="x" * 190))
    assert len(out["blocking"]) == 10
    assert out["reason"].endswith("more premise row(s)")


def _counts(out: dict) -> tuple:
    return out["blocking_findings"], out["advisory_findings"]


def _with_gate(coverage, reviews, root=None, rows=None) -> dict:
    return op.readiness_verdict(_gate(PASS, verdict="PREPPED"), coverage, reviews, root, rows)


def test_warn_review_with_two_uncleared_rows_plus_coverage_findings_reads_0_blocking_19_advisory(tmp_path):
    ledger = _ledger(tmp_path, [{"id": "F1"}, {"id": "F2", "verified": True}, {"id": "F3", "verified": False}])
    cov = {"verdict": "pull", "mechanicalFindings": [], "judgmentFindings": 17}
    out = _with_gate(cov, _review("WARN", ledger), tmp_path)
    assert _counts(out) == (0, 19)
    assert out["reason"].startswith("needs an agent: 0 blocking, 19 advisory: ")
    assert len(out["reason"]) <= 1000
    assert out["blocking_findings"] != len(out["blocking"])
    _validate(out | {"plan": REL, "prep_gate": _gate(PASS, verdict="PREPPED")})


def test_blocked_review_with_unverified_ledger_counts_blocking(tmp_path):
    ledger = _ledger(tmp_path, [{"id": "F1"}])
    assert _counts(_with_gate(None, _review("BLOCKED", ledger), tmp_path)) == (1, 0)
    assert _counts(_with_gate(None, _review("BLOCKED"), tmp_path)) == (1, 0)
    assert _counts(_with_gate(None, _review("reject", "missing.md"), tmp_path)) == (1, 0)


def test_blocked_review_with_verified_ledger_beside_uncleared_warn_is_0_blocking(tmp_path):
    ok = _ledger(tmp_path, [{"id": "F1", "verified": True}], name="a.md")
    bad = _ledger(tmp_path, [{"id": "F1"}], name="b.md")
    reviews = [
        {"reviewer": "a", "verdict": "BLOCKED", "ledger": ok},
        {"reviewer": "b", "verdict": "WARN", "ledger": bad},
    ]
    assert _counts(_with_gate(None, reviews, tmp_path)) == (0, 1)


def test_stamped_all_verified_ledger_on_unrecognised_verdict_still_adds_one(tmp_path):
    ledger = _ledger(tmp_path, [{"id": "F1", "verified": True}])
    assert _counts(_with_gate(None, _review("mystery", ledger), tmp_path)) == (1, 0)


def test_blocked_surface_to_pm_coverage_counts_judgment_findings_as_blocking():
    cov = {"verdict": "BLOCKED-SURFACE-TO-PM", "mechanicalFindings": [], "judgmentFindings": ["a", "b", "c"]}
    assert _counts(_with_gate(cov, [])) == (3, 0)


def test_premise_contradicted_row_is_one_blocking_and_a_covered_row_is_zero(tmp_path):
    assert _counts(_premise([_row("CONTRADICTED")])) == (1, 0)
    common = _corrects_plan(tmp_path, _CORRECTS)
    reply = _op_reply(common, [_row()])
    assert _counts(reply) == (0, 0)
    assert reply["verdict"] == "ready"


def test_ready_reply_carries_zero_counts():
    out = _with_gate(None, [])
    assert out["verdict"] == "ready"
    assert _counts(out) == (0, 0)


def test_substantive_prep_gate_failure_yields_one_prep_gate_ruling():
    out = op.readiness_verdict(_gate(MECH, JUDG), None, [])
    assert [(r["id"], r["source"]) for r in out["rulings"]] == [("J1", "prep-gate")]
    assert "census-incomplete" in out["rulings"][0]["subject"]
    assert out["split_recommended"] is False
    _validate({"plan": REL, **out, "prep_gate": _gate(MECH, JUDG)})


def test_warn_review_and_unresolved_premise_yield_review_and_premise_rulings():
    out = op.readiness_verdict(
        _gate(PASS, verdict="PREPPED"), None,
        [{"reviewer": "patrik", "verdict": "WARN"}], None, _rows("UNRESOLVED", citation="x"),
    )
    assert [(r["id"], r["source"]) for r in out["rulings"]] == [("J1", "review"), ("J2", "premise")]
    assert "declared_absent" in out["rulings"][1]["ask"]


def test_ready_and_mechanical_verdicts_have_no_rulings_and_no_split():
    ready = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), None, [])
    assert (ready["rulings"], ready["split_recommended"]) == ([], False)
    mech = op.readiness_verdict(_gate(PASS, MECH), None, [])
    assert (mech["rulings"], mech["split_recommended"]) == ([], False)


@pytest.mark.parametrize("count,split", [(op.SPLIT_BLOCKING_THRESHOLD - 1, False), (op.SPLIT_BLOCKING_THRESHOLD, True)])
def test_split_recommended_flips_at_the_threshold(count, split):
    cov = {"verdict": "BLOCKED-SURFACE-TO-PM", "mechanicalFindings": [], "judgmentFindings": count}
    out = op.readiness_verdict(_gate(PASS, verdict="PREPPED"), cov, [])
    assert out["blocking_findings"] == count
    assert out["split_recommended"] is split
