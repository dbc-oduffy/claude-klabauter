"""
coordinator_core.ops.tests.test_deferral_detect_orphan_memo

Tests for the pure classify_orphan_memos() decision core of the
"deferral.detect_orphan_memo" op, plus the registered-op wiring (_handler)
against a tmp_path fixture repo tree.

Coverage (acceptance-mapped per tasks/hidden-deferral-detectors/design.md §
Detector 2):
  (a) open+ask+aging+unowned memo -> flagged ("orphans_found", offer present).
  (b) open+proposal+aging+OWNED memo (source_memo: reference in a plan,
      replicating the memo-tool-rebuild ownership pattern) -> NOT flagged.
  (c) fyi/consult kind -> NOT flagged (kind not in {ask, proposal}).
  (d) status: actioned -> NOT flagged.
  (e) age < threshold -> NOT flagged.
  (f) no inbox dir / empty inbox -> clean, no crash.
  (g) _handler wiring smoke test via direct call with repo_root=tmp_path.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

from coordinator_core.ops.deferral_detect_orphan_memo import (
    _SLUG_OWNING_GLOBS,
    _handler,
    _read_inbox_memos,
    _read_owning_text,
    _topic_slug,
    classify_orphan_memos,
)

TODAY = date(2026, 7, 21)


def _memo(basename, kind="ask", status="open", created="2026-07-17", frm="claude-central-em"):
    return {"basename": basename, "kind": kind, "status": status, "created": created, "from": frm}


def _write_memo(inbox_dir: Path, basename: str, *, kind="ask", status="open",
                 created="2026-07-17", frm="claude-central-em", title="A memo") -> Path:
    inbox_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f'title: "{title}"\n'
        f'from: "{frm}"\n'
        f"to: claude-klabauter-em\n"
        f"created: {created}\n"
        f"status: {status}\n"
        f'kind: "{kind}"\n'
        "---\n\n"
        "## Body\n\nSome content.\n"
    )
    path = inbox_dir / basename
    path.write_text(text, encoding="utf-8")
    return path


def _write_plan(plans_dir: Path, name: str, *, source_memo=None, body_ref=None) -> Path:
    plans_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---", "title: \"A plan\"", "created: 2026-07-21"]
    if source_memo:
        fm_lines.append(f"source_memo: {source_memo}")
    fm_lines.append("---")
    body = "\n# A plan\n"
    if body_ref:
        body += f"\nReferences {body_ref} in the body.\n"
    text = "\n".join(fm_lines) + body
    path = plans_dir / name
    path.write_text(text, encoding="utf-8")
    return path


class TestTopicSlug:
    def test_strips_date_prefix_and_extension(self):
        assert (
            _topic_slug("2026-07-17-claude-central-em-claude-klabauter-owns-cross-repo-memo-tool.md")
            == "claude-central-em-claude-klabauter-owns-cross-repo-memo-tool"
        )

    def test_falls_back_when_no_date_prefix(self):
        assert _topic_slug("no-date-here.md") == "no-date-here"


class TestClassifyOrphanMemosCore:
    def test_open_ask_aging_unowned_is_flagged(self):
        memos = [_memo("2026-07-17-orphan-ask.md", kind="ask")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result["state"] == "orphans_found"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["basename"] == "2026-07-17-orphan-ask.md"

    def test_orphan_offer_present_and_summarizes(self):
        memos = [_memo("2026-07-17-orphan-ask.md", kind="proposal")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert "offer" in result
        assert "2026-07-17-orphan-ask.md" in result["offer"]
        assert "kind:proposal" in result["offer"]

    def test_owned_by_basename_reference_not_flagged(self):
        basename = "2026-07-17-claude-central-em-claude-klabauter-owns-cross-repo-memo-tool.md"
        memos = [_memo(basename, kind="proposal")]
        owning_text = f"source_memo: {basename}\n"
        result = classify_orphan_memos(memos, owning_text=owning_text, today=TODAY)
        assert result == {"state": "clean"}

    def test_owned_by_topic_slug_reference_not_flagged(self):
        basename = "2026-07-17-some-topic-slug.md"
        memos = [_memo(basename, kind="ask")]
        owning_text = "This plan handles the some-topic-slug work.\n"
        result = classify_orphan_memos(memos, owning_text=owning_text, today=TODAY)
        assert result == {"state": "clean"}

    def test_fyi_kind_not_flagged(self):
        memos = [_memo("2026-07-17-fyi.md", kind="fyi")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result == {"state": "clean"}

    def test_consult_kind_not_flagged(self):
        memos = [_memo("2026-07-17-consult.md", kind="consult")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result == {"state": "clean"}

    def test_default_kind_is_ask_when_absent(self):
        memos = [_memo("2026-07-17-default-kind.md", kind="")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result["state"] == "orphans_found"
        assert result["findings"][0]["kind"] == "ask"

    def test_status_actioned_not_flagged(self):
        memos = [_memo("2026-07-17-actioned.md", status="actioned")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result == {"state": "clean"}

    def test_age_below_threshold_not_flagged(self):
        memos = [_memo("2026-07-20-too-young.md", created="2026-07-20")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY, age_threshold_days=3)
        assert result == {"state": "clean"}

    def test_age_exactly_at_threshold_is_flagged(self):
        memos = [_memo("2026-07-18-at-threshold.md", created="2026-07-18")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY, age_threshold_days=3)
        assert result["state"] == "orphans_found"

    def test_custom_age_threshold_param(self):
        memos = [_memo("2026-07-17-custom-threshold.md", created="2026-07-17")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY, age_threshold_days=10)
        assert result == {"state": "clean"}

    def test_missing_created_date_skipped_no_crash(self):
        memos = [_memo("2026-07-17-no-date.md", created="")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result == {"state": "clean"}

    def test_malformed_created_date_skipped_no_crash(self):
        memos = [_memo("2026-07-17-bad-date.md", created="not-a-date")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result == {"state": "clean"}

    def test_empty_memos_iterable_is_clean(self):
        result = classify_orphan_memos([], owning_text="", today=TODAY)
        assert result == {"state": "clean"}

    def test_multiple_orphans_all_findings_present(self):
        memos = [
            _memo("2026-07-17-first.md", kind="ask"),
            _memo("2026-07-16-second.md", kind="proposal", created="2026-07-16"),
        ]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result["state"] == "orphans_found"
        assert len(result["findings"]) == 2
        basenames = {f["basename"] for f in result["findings"]}
        assert basenames == {"2026-07-17-first.md", "2026-07-16-second.md"}

    def test_slug_substring_inside_unrelated_word_not_owned(self):
        """Review: code-reviewer Finding 2 — a generic slug must not match as
        a plain substring of an unrelated longer word; the match has to be
        word/token-boundary-anchored."""
        basename = "2026-07-17-list.md"
        memos = [_memo(basename, kind="ask")]
        # "list" is a substring of "checklist" but not a word-boundary match.
        owning_text = "See the project checklist for details.\n"
        result = classify_orphan_memos(memos, owning_text=owning_text, today=TODAY)
        assert result["state"] == "orphans_found"

    def test_slug_word_boundary_match_still_owned(self):
        basename = "2026-07-17-list.md"
        memos = [_memo(basename, kind="ask")]
        owning_text = "This plan handles the list rollout.\n"
        result = classify_orphan_memos(memos, owning_text=owning_text, today=TODAY)
        assert result == {"state": "clean"}

    def test_basename_anchored_match_still_owns(self):
        """Basename false-positive collision is structurally rare (hyphens
        in the basename already create word boundaries around most
        embeddings), so this pins the anchoring is applied without
        regressing the legitimate-match path — code-reviewer Finding 2's
        "anchor it too if cheap" for the basename branch."""
        basename = "2026-07-17-widget.md"
        memos = [_memo(basename, kind="ask")]
        owning_text = f"source_memo: {basename}\n"
        result = classify_orphan_memos(memos, owning_text=owning_text, today=TODAY)
        assert result == {"state": "clean"}

    def test_future_created_date_still_flagged_not_silently_clean(self):
        """Review: code-reviewer Finding 7 — a fat-fingered future `created:`
        date yields negative age_days; it must not silently suppress a real
        orphan by never clearing the age threshold."""
        memos = [_memo("2026-07-17-future.md", kind="ask", created="2099-01-01")]
        result = classify_orphan_memos(memos, owning_text="", today=TODAY)
        assert result["state"] == "orphans_found"
        finding = result["findings"][0]
        assert finding["age_days"] < 0
        assert "suspicious future" in finding["offer"]

    def test_future_created_date_still_respects_ownership(self):
        basename = "2026-07-17-future-owned.md"
        memos = [_memo(basename, kind="ask", created="2099-01-01")]
        owning_text = f"source_memo: {basename}\n"
        result = classify_orphan_memos(memos, owning_text=owning_text, today=TODAY)
        assert result == {"state": "clean"}


class TestReadInboxMemos:
    def test_missing_inbox_dir_returns_empty_no_crash(self, tmp_path):
        assert _read_inbox_memos(tmp_path / "cross-repo" / "inbox") == ([], False)

    def test_empty_inbox_dir_returns_empty(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        assert _read_inbox_memos(inbox) == ([], False)

    def test_parses_frontmatter_scalars(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-17-a.md", kind="proposal", status="open",
                    created="2026-07-17", frm="claude-central-em")
        memos, degraded = _read_inbox_memos(inbox)
        assert degraded is False
        assert len(memos) == 1
        assert memos[0]["basename"] == "2026-07-17-a.md"
        assert memos[0]["kind"] == "proposal"
        assert memos[0]["status"] == "open"
        assert memos[0]["created"] == "2026-07-17"
        assert memos[0]["from"] == "claude-central-em"

    def test_ignores_non_md_files(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "README.txt").write_text("not a memo", encoding="utf-8")
        assert _read_inbox_memos(inbox) == ([], False)


class TestReadOwningText:
    def test_missing_dirs_returns_empty_string_no_crash(self, tmp_path):
        assert _read_owning_text(tmp_path) == ("", [])

    def test_concatenates_plans_handoffs_decisions(self, tmp_path):
        _write_plan(tmp_path / "docs" / "plans", "2026-07-21-a-plan.md",
                    source_memo="2026-07-17-owned.md")
        handoffs = tmp_path / "state" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "2026-07-20_a.md").write_text("mentions 2026-07-16-handoff-owned.md\n",
                                                    encoding="utf-8")
        decisions = tmp_path / "docs" / "decisions"
        decisions.mkdir(parents=True)
        (decisions / "DR-999.md").write_text("references 2026-07-15-decision-owned.md\n",
                                               encoding="utf-8")

        blob, scan_errors = _read_owning_text(tmp_path)
        assert scan_errors == []
        assert "2026-07-17-owned.md" in blob
        assert "2026-07-16-handoff-owned.md" in blob
        assert "2026-07-15-decision-owned.md" in blob

    def test_slug_owning_globs_excludes_handoffs(self, tmp_path):
        """Review: code-reviewer Finding 3 — the slug-eligible blob
        (_SLUG_OWNING_GLOBS) must exclude state/handoffs/*.md."""
        handoffs = tmp_path / "state" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "2026-07-20_a.md").write_text(
            "handoff-only-marker mentioned in passing\n", encoding="utf-8"
        )
        _write_plan(tmp_path / "docs" / "plans", "2026-07-21-a-plan.md",
                    body_ref="plan-only-marker")

        blob, scan_errors = _read_owning_text(tmp_path, _SLUG_OWNING_GLOBS)
        assert scan_errors == []
        assert "handoff-only-marker" not in blob
        assert "plan-only-marker" in blob


class TestHandoffOwnershipScoping:
    """Review: code-reviewer Finding 3 — a memo merely name-dropped in
    passing by a handoff must NOT read as OWNED via the loose topic-slug
    match; only a full-basename reference in a handoff counts."""

    def test_slug_name_drop_in_handoff_does_not_own(self):
        basename = "2026-07-17-some-topic-slug.md"
        memos = [_memo(basename, kind="ask")]
        full_owning_text = "Handoff prose: read the some-topic-slug memo, noted it.\n"
        # owning_text_slug excludes handoffs (production _handler behavior);
        # here we simulate that by passing an empty slug-eligible blob while
        # the full (basename-eligible) blob still contains the handoff prose.
        result = classify_orphan_memos(
            memos, owning_text=full_owning_text, today=TODAY, owning_text_slug=""
        )
        assert result["state"] == "orphans_found"

    def test_full_basename_reference_in_handoff_still_owns(self):
        basename = "2026-07-17-real-tracked-memo.md"
        memos = [_memo(basename, kind="ask")]
        full_owning_text = f"Handoff prose: tracked {basename} directly.\n"
        result = classify_orphan_memos(
            memos, owning_text=full_owning_text, today=TODAY, owning_text_slug=""
        )
        assert result == {"state": "clean"}

    def test_handler_end_to_end_handoff_name_drop_not_owned(self, tmp_path):
        """End-to-end via _handler: a handoff that only name-drops the
        memo's topic slug in prose (no full basename, no plans/decisions
        reference) must NOT suppress the orphan finding."""
        basename = "2026-07-17-name-dropped.md"
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, basename, kind="ask", status="open", created="2026-07-17")
        handoffs = tmp_path / "state" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "2026-07-20_a.md").write_text(
            "Discussed the name-dropped topic in passing.\n", encoding="utf-8"
        )

        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result["state"] == "orphans_found"

    def test_handler_end_to_end_handoff_full_basename_reference_owns(self, tmp_path):
        basename = "2026-07-17-really-tracked.md"
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, basename, kind="ask", status="open", created="2026-07-17")
        handoffs = tmp_path / "state" / "handoffs"
        handoffs.mkdir(parents=True)
        (handoffs / "2026-07-20_a.md").write_text(
            f"Tracked {basename} explicitly in this handoff.\n", encoding="utf-8"
        )

        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result == {"state": "clean"}


class TestHandlerWiring:
    def test_flags_orphan_via_handler(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-17-orphan.md", kind="ask", status="open",
                    created="2026-07-17")

        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result["state"] == "orphans_found"
        assert result["findings"][0]["basename"] == "2026-07-17-orphan.md"

    def test_owned_memo_reads_clean_via_handler(self, tmp_path):
        basename = "2026-07-17-claude-central-em-claude-klabauter-owns-cross-repo-memo-tool.md"
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, basename, kind="proposal", status="open", created="2026-07-17")
        _write_plan(tmp_path / "docs" / "plans",
                    "2026-07-21-memo-tool-rebuild-full-ownership.md",
                    source_memo=basename)

        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_fyi_status_actioned_and_too_young_all_clean_via_handler(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-17-fyi.md", kind="fyi", status="open", created="2026-07-17")
        _write_memo(inbox, "2026-07-17-actioned.md", kind="ask", status="actioned",
                    created="2026-07-17")
        _write_memo(inbox, "2026-07-20-young.md", kind="ask", status="open", created="2026-07-20")

        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_no_inbox_dir_clean_no_crash_via_handler(self, tmp_path):
        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_empty_inbox_dir_clean_no_crash_via_handler(self, tmp_path):
        (tmp_path / "cross-repo" / "inbox").mkdir(parents=True)
        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_default_age_threshold_applied_via_handler(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-19-borderline.md", kind="ask", status="open",
                    created="2026-07-19")
        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_custom_age_threshold_param_via_handler(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-17-custom.md", kind="ask", status="open",
                    created="2026-07-17")
        result = _handler({"today": "2026-07-21", "age_threshold_days": 10},
                                repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_repo_root_none_falls_back_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _handler({"today": "2026-07-21"}, repo_root=None)
        assert result == {"state": "clean"}

    def test_string_age_threshold_param_coerced_via_handler(self, tmp_path):
        """Review: the Staff Engineer Finding 6 — age_threshold_days must be coerced to
        int; a string (plausible on the standalone `python3 -m` path) would
        otherwise TypeError on `age_days < age_threshold_days`."""
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-17-str-threshold.md", kind="ask", status="open",
                    created="2026-07-17")
        result = _handler({"today": "2026-07-21", "age_threshold_days": "10"},
                                repo_root=tmp_path)
        assert result == {"state": "clean"}

    def test_non_coercible_age_threshold_param_falls_back_to_default(self, tmp_path):
        inbox = tmp_path / "cross-repo" / "inbox"
        _write_memo(inbox, "2026-07-17-bad-threshold.md", kind="ask", status="open",
                    created="2026-07-17")
        result = _handler(
            {"today": "2026-07-21", "age_threshold_days": "not-a-number"},
            repo_root=tmp_path,
        )
        # falls back to the default threshold (3d); memo is 4d old -> flagged.
        assert result["state"] == "orphans_found"


# ---------------------------------------------------------------------------
# Unscannable owning-artifact directory — silent-success guard
# (silent-enumeration audit). Path.glob() silently swallows PermissionError
# even on a flat, non-recursive pattern (empirically re-verified: a
# chmod-000 dir yields an empty iterator from glob(), no exception) — a
# dropped owning dir must not read as "this memo has no owning artifact"
# (a false ORPHANED verdict); it must downgrade to "indeterminate".
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_read_owning_text_unreadable_plans_dir_reports_scan_error(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-07-21-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = plans_dir.stat().st_mode
    os.chmod(plans_dir, 0o000)
    try:
        blob, scan_errors = _read_owning_text(tmp_path)
    finally:
        os.chmod(plans_dir, original_mode)

    assert blob == ""
    assert scan_errors, "scan_errors must be non-empty when docs/plans/ cannot be listed"
    assert any(str(plans_dir) in e for e in scan_errors)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_handler_downgrades_to_indeterminate_when_owning_dir_unreadable(tmp_path):
    """A dropped docs/plans/ (the actual owning artifact) must not read as
    a confident orphan — the memo IS owned by a plan under the unscanned
    directory, but the scan failure must surface as indeterminate, not a
    silently-wrong "orphans_found" OR a silently-wrong "clean"."""
    basename = "2026-07-17-would-be-owned.md"
    inbox = tmp_path / "cross-repo" / "inbox"
    _write_memo(inbox, basename, kind="ask", status="open", created="2026-07-17")

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    _write_plan(plans_dir, "2026-07-21-owning-plan.md", source_memo=basename)

    original_mode = plans_dir.stat().st_mode
    os.chmod(plans_dir, 0o000)
    try:
        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
    finally:
        os.chmod(plans_dir, original_mode)

    assert result["state"] == "indeterminate", (
        "an unscannable owning-artifact directory must downgrade the result "
        f"to indeterminate, not assert a confident verdict — got {result!r}"
    )
    assert result.get("scan_errors"), "scan_errors must be carried onto the indeterminate result"


def test_handler_state_clean_when_scan_fully_succeeds(tmp_path):
    result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
    assert result == {"state": "clean"}


# ---------------------------------------------------------------------------
# Unscannable cross-repo/inbox/ — silent-enumeration audit for the PRIMARY
# input surface (Review: code-reviewer Finding 2). `os.listdir` DOES raise
# `PermissionError` correctly here (unlike glob()), but the previous
# `_read_inbox_memos` converted that raised exception into a bare `[]` with
# zero signal propagated to `_handler`, so `classify_orphan_memos([], ...)`
# returned a confident "clean" verdict for a scan that never actually ran —
# a much higher-severity miss than an unreadable owning-artifact dir, since
# it silently suppresses the entire detector rather than merely risking a
# false orphan.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_read_inbox_memos_unreadable_dir_reports_degraded(tmp_path):
    inbox = tmp_path / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "2026-07-21-unreachable.md").write_text("unused", encoding="utf-8")

    original_mode = inbox.stat().st_mode
    os.chmod(inbox, 0o000)
    try:
        memos, degraded = _read_inbox_memos(inbox)
    finally:
        os.chmod(inbox, original_mode)

    assert memos == []
    assert degraded is True, "degraded must be True when cross-repo/inbox/ cannot be listed"


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_handler_downgrades_to_indeterminate_when_inbox_unreadable(tmp_path):
    """An unreadable cross-repo/inbox/ must downgrade the result to
    "indeterminate", NEVER a confident "clean" — a dropped inbox means the
    detector cannot even enumerate its primary input surface."""
    inbox = tmp_path / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "2026-07-21-would-be-orphan.md").write_text("unused", encoding="utf-8")

    original_mode = inbox.stat().st_mode
    os.chmod(inbox, 0o000)
    try:
        result = _handler({"today": "2026-07-21"}, repo_root=tmp_path)
    finally:
        os.chmod(inbox, original_mode)

    assert result["state"] == "indeterminate", (
        "an unscannable cross-repo/inbox/ must downgrade the result to "
        f"indeterminate, never a confident verdict — got {result!r}"
    )
    assert result.get("scan_errors"), "scan_errors must be carried onto the indeterminate result"
