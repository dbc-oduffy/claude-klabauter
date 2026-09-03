"""Tests for coordinator_core.subagent_sandbox.detect_unfilled_sidecar --
the detection leg of the run-report sidecar contract (2026-08-15 incident,
see that module's docstring)."""

from __future__ import annotations

import os

from coordinator_core.subagent_sandbox.detect_unfilled_sidecar import (
    is_unfilled_body,
    scan_paths,
    scan_session_dir,
)
from coordinator_core.subagent_sandbox.provision_report import (
    _build_assessment_doc_text,
    _build_review_findings_doc_text,
    _build_run_report_doc_text,
    _build_run_report_legacy_doc_text,
    _build_staff_eng_review_doc_text,
)


def test_pristine_run_report_scaffold_is_unfilled():
    text = _build_run_report_doc_text("coordinator:executor", "t", "abc")
    assert is_unfilled_body(text) is True


def test_pristine_staff_eng_review_scaffold_is_unfilled():
    text = _build_staff_eng_review_doc_text("coordinator:staff-eng-reviewer", "t", "abc")
    assert is_unfilled_body(text) is True


def test_pristine_review_findings_scaffold_is_unfilled():
    text = _build_review_findings_doc_text("coordinator:reviewer", "t", "abc")
    assert is_unfilled_body(text) is True


def test_run_notes_content_flips_to_filled():
    text = _build_run_report_doc_text("coordinator:executor", "t", "abc")
    text = text.replace("## Run notes\n\n", "## Run notes\n\nDid a thing.\n\n")
    assert is_unfilled_body(text) is False


def test_checked_completion_box_alone_does_not_count_as_filled():
    text = _build_run_report_doc_text("coordinator:executor", "t", "abc")
    text = text.replace("- [ ] Complete", "- [x] Complete")
    assert is_unfilled_body(text) is False


def test_genuine_unchecked_todo_is_not_stripped_as_scaffold():
    # Regression for the P2 finding: only the scaffold's own literal
    # checkbox line is stripped, so an agent's genuine unchecked TODO --
    # even if it is the sidecar's only authored content -- must survive the
    # strip and flip the verdict to filled, not read as an unfilled sidecar.
    text = _build_run_report_doc_text("coordinator:executor", "t", "abc")
    text = text.replace(
        "## Run notes\n\n",
        "## Run notes\n\n- [ ] follow-up: verify X before merge\n\n",
    )
    assert is_unfilled_body(text) is False


def test_pristine_assessment_scaffold_is_unfilled():
    text = _build_assessment_doc_text("coordinator:executor", "t", "abc")
    assert is_unfilled_body(text) is True


def test_answered_assessment_flips_to_filled():
    text = _build_assessment_doc_text("coordinator:executor", "t", "abc")
    text = text.replace("## Questions\n\n", "## Questions\n\n- Q: ok? / A: yes.\n\n")
    assert is_unfilled_body(text) is False


def test_pristine_legacy_no_type_key_scaffold_is_unfilled():
    text = _build_run_report_legacy_doc_text("coordinator:executor", "t", "abc")
    assert is_unfilled_body(text) is True


def test_filled_legacy_no_type_key_scaffold_flips_to_filled():
    text = _build_run_report_legacy_doc_text("coordinator:executor", "t", "abc")
    text = text.replace("## Observations\n\n", "## Observations\n\nSaw the thing.\n\n")
    assert is_unfilled_body(text) is False


def test_answered_exit_interview_alone_flips_to_filled():
    text = _build_run_report_doc_text("coordinator:executor", "t", "abc")
    text = text.replace(
        "- Anything you wanted to say and had nowhere to put?\n\n",
        "- Anything you wanted to say and had nowhere to put?\n\nYes, X was weird.\n\n",
    )
    assert is_unfilled_body(text) is False


def test_scan_paths_flags_open_and_unfilled(tmp_path):
    unfilled_path = tmp_path / "unfilled.md"
    unfilled_path.write_text(
        _build_run_report_doc_text("coordinator:executor", "t", "abc"), encoding="utf-8"
    )

    filled_text = _build_run_report_doc_text("coordinator:executor", "t", "abc").replace(
        "## Run notes\n\n", "## Run notes\n\nDid the thing.\n\n"
    )
    filled_path = tmp_path / "filled.md"
    filled_path.write_text(filled_text, encoding="utf-8")

    closed_text = _build_run_report_doc_text("coordinator:executor", "t", "abc").replace(
        "status: open", "status: complete"
    )
    closed_path = tmp_path / "closed.md"
    closed_path.write_text(closed_text, encoding="utf-8")

    verdicts = scan_paths([str(unfilled_path), str(filled_path), str(closed_path)])
    flagged = {v.path for v in verdicts if v.flagged}

    assert flagged == {str(unfilled_path)}


def test_scan_session_dir_reads_the_named_session_only(tmp_path):
    share = tmp_path / ".coordinator-local" / "subagent-share"
    session_a = share / "session-a"
    session_b = share / "session-b"
    session_a.mkdir(parents=True)
    session_b.mkdir(parents=True)

    (session_a / "one.md").write_text(
        _build_run_report_doc_text("coordinator:executor", "t", "abc"), encoding="utf-8"
    )
    (session_b / "two.md").write_text(
        _build_run_report_doc_text("coordinator:executor", "t", "abc"), encoding="utf-8"
    )

    verdicts = scan_session_dir(str(tmp_path), "session-a")

    assert len(verdicts) == 1
    assert os.path.basename(verdicts[0].path) == "one.md"
    assert verdicts[0].flagged is True


def test_scan_session_dir_rejects_traversal_outside_subagent_share(tmp_path):
    share = tmp_path / ".coordinator-local" / "subagent-share"
    share.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(
        _build_run_report_doc_text("coordinator:executor", "t", "abc"), encoding="utf-8"
    )

    verdicts = scan_session_dir(str(tmp_path), os.path.join("..", "outside"))

    assert verdicts == []
