"""Tests for the ``key_results[]`` line scraper in ``coordinator_core.goals.reassess_krs``.

Regression home for the 2026-09-06 defect reported by doe-claude-em
(`state/cross-repo/inbox/2026-09-06-doe-claude-em-reassess-goal-krs-inline-enum-comment-defeats-the-only-transition.md`):
docgen's `goal` template scaffolds `status: not-started  # not-started | ...`,
the scraper captured that hint into the value, and `process_kr_entry`'s exact
compare then made the op's only transition unreachable.
"""

from __future__ import annotations

from coordinator_core.goals.reassess_krs import (
    KR_STATUS_ENUM,
    extract_key_results,
    parse_kr_block,
    process_kr_entry,
    strip_inline_comment,
)

# The literal docgen `goal` template emits — see
# coordinator_core/ops/docgen/templates/goal.json.
SCAFFOLDED_GOAL = """schema: goal
id: "goal-scaffolded"
title: "A scaffolded goal"
status: active
key_results:
  - id: kr-1
    text: "ship the widget pipeline"
    kind: outcome  # output | outcome
    status: not-started  # not-started | in-progress | met | at-risk
    weekly_perceptible: true
    evidence_source: null
"""


def test_strip_inline_comment_drops_the_enum_hint():
    assert (
        strip_inline_comment("not-started  # not-started | in-progress | met | at-risk")
        == "not-started"
    )


def test_strip_inline_comment_keeps_a_hash_inside_quotes():
    assert strip_inline_comment('"close out issue #143"') == '"close out issue #143"'


def test_strip_inline_comment_keeps_a_hash_with_no_leading_whitespace():
    """A `#` not preceded by whitespace does not open a YAML comment."""
    assert strip_inline_comment("colour#ff0000") == "colour#ff0000"


def test_strip_inline_comment_handles_a_whole_line_comment_value():
    assert strip_inline_comment("# nothing but a comment") == ""


def test_strip_inline_comment_is_a_no_op_without_a_comment():
    assert strip_inline_comment("in-progress") == "in-progress"


def test_scaffolded_status_parses_to_the_bare_enum_member():
    entries = parse_kr_block(extract_key_results(SCAFFOLDED_GOAL))
    assert len(entries) == 1
    kr_id, kr_text, kr_status, kr_weekly = entries[0]
    assert kr_id == "kr-1"
    assert kr_status == "not-started"
    assert kr_status in KR_STATUS_ENUM
    assert kr_weekly == "true"
    # The quoted text keeps its quotes — the _extract_keywords parity quirk
    # depends on that, and comment-stripping must not disturb it.
    assert kr_text == '"ship the widget pipeline"'


def test_scaffolded_goal_can_reach_in_progress_on_movement():
    """The defect: this transition was unreachable for any scaffolded goal."""
    _, _, kr_status, kr_weekly = parse_kr_block(extract_key_results(SCAFFOLDED_GOAL))[0]
    result = process_kr_entry(
        "kr-1", "ship the widget pipeline", kr_status, kr_weekly, "shipped the widget pipeline"
    )
    assert result is not None
    assert result["movement"] == "yes"
    assert result["proposed_status"] == "in-progress"
    assert result["anomalies"] == []


def test_off_enum_status_is_reported_not_passed_through():
    result = process_kr_entry("kr-1", "ship it", "not-started  # hint", "true", "shipped it")
    assert result is not None
    assert len(result["anomalies"]) == 1
    assert "not one of" in result["anomalies"][0]
    assert "OFF-ENUM FIELD" in result["report_line"]


def test_non_boolean_weekly_perceptible_is_reported():
    result = process_kr_entry("kr-1", "ship it", "not-started", "true  # bool", "no signal here")
    assert result is not None
    assert any("weekly_perceptible" in a for a in result["anomalies"])
    assert "OFF-ENUM FIELD" in result["report_line"]


def test_clean_kr_reports_no_anomalies():
    result = process_kr_entry("kr-1", "ship it", "in-progress", "false", "")
    assert result is not None
    assert result["anomalies"] == []
    assert "OFF-ENUM FIELD" not in result["report_line"]


def test_inline_dash_line_fields_are_comment_stripped():
    block = extract_key_results(
        "key_results:\n  - id: kr-2  # the second one\n    status: met  # done\n"
    )
    kr_id, _, kr_status, _ = parse_kr_block(block)[0]
    assert kr_id == "kr-2"
    assert kr_status == "met"


def test_off_enum_anomaly_reaches_the_op_warnings(tmp_path):
    from coordinator_core.goals.reassess_krs import reassess

    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "g.yaml").write_text(
        "schema: goal\nid: g-1\ntitle: T\nstatus: active\n"
        "key_results:\n  - id: kr-1\n    text: ship it\n    status: nonsense\n"
        "    weekly_perceptible: true\n",
        encoding="utf-8",
    )
    result = reassess(goals_dir, since="7d", dry_run=True)
    assert any("nonsense" in w and "g.yaml" in w for w in result["warnings"])
