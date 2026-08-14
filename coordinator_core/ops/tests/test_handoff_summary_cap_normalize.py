"""
Tests for the C1 break-class fix (2026-08-13, unwritable-handoff-records-
fail-loudly): `_normalize_one_text`'s summary normalization (#4) now caps a
PRESENT, over-cap `summary:` too, not only one it backfills from the H1.

Spec: docs/plans/2026-08-13-unwritable-handoff-records-fail-loudly.md § C1
Model: coordinator_core.ops.memo_transition._normalize_oversize_summary /
       _normalize_block_scalar_summary

Negative-spec: does NOT touch `schema_validate._cf_summary_length_cap` (AC-3)
— that gate stays strict; these tests assert the seam normalizes AHEAD of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (CBR #12)
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops import handoff_normalize
from coordinator_core.ops.handoff_normalize import _normalize_one_text
from coordinator_core.ops.handoff_transition import _SCHEMA_PATH

# Reuse the existing git-repo/asyncio-run helpers rather than duplicating them
# (both test modules live in the same coordinator_core.ops.tests package).
from coordinator_core.ops.tests.test_handoff_author_fork import (
    _make_git_repo,
    _run as _run_fork,
)
from coordinator_core.ops.handoff_author_fork import _handler as _fork_handler
from coordinator_core.ops.handoff_transition import _handler as _transition_handler

_FILE_PATH = Path("state/handoffs/x.md")


def _fm(summary_line: str) -> str:
    return f"""\
---
title: Some handoff
created: 2026-08-13
pickup_ready: true
category: infra
{summary_line}
deliverable_id: dlv-existing-123abc
initiative: null
owner: dbc-em-session-xyz
author: dbc-em-session-xyz
---

# Some handoff

Body.
"""


def test_present_over_cap_summary_truncates_and_warns(capsys):
    over_cap = "S" * 201
    content = _fm(f"summary: {over_cap}")

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    summary = handoff_normalize.unquote_yaml_scalar(
        handoff_normalize.read_fm_field(fm_text, "summary")
    )
    assert len(summary) == 140
    assert summary.endswith("…")
    assert any("summary" in c and "truncated" in c for c in result["changes"])

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "201" in err  # original length


def test_at_cap_summary_is_byte_identical_no_warning(capsys):
    at_cap = "S" * 140
    content = _fm(f"summary: {at_cap}")

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is None
    assert capsys.readouterr().err == ""


def test_under_cap_summary_is_byte_identical_no_warning(capsys):
    under_cap = "S" * 50
    content = _fm(f"summary: {under_cap}")

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is None
    assert capsys.readouterr().err == ""


def test_present_summary_with_trailing_comment_is_measured_by_decoded_value(capsys):
    """Regression for the P1 measurement-mismatch finding (code-reviewer,
    2026-08-13): the gate (`_cf_summary_length_cap`) measures the
    yaml.safe_load-DECODED value, which never includes a trailing `# comment`
    — a comment is not part of the scalar. The raw on-disk text DOES include
    it. A summary well under cap, with a long trailing comment pushing the
    RAW text over 140 chars, must NOT be truncated: measuring raw text (the
    pre-fix bug) would falsely trip the >140 branch and mangle the comment."""
    short_summary = "S" * 30
    long_comment = "x" * 150
    content = _fm(f"summary: {short_summary}  # {long_comment}")

    result = _normalize_one_text(content, _FILE_PATH)

    # The full line (value + comment) is well over 140 raw chars, but the
    # decoded scalar is only 30 — no drift, no truncation, no warning.
    assert result is None
    assert capsys.readouterr().err == ""


def test_present_summary_with_double_quoted_backslash_escape_is_measured_by_decoded_value(capsys):
    """A double-quoted summary containing a backslash escape (`\\n`) decodes
    to a SHORTER string than its raw on-disk text (the two-char `\\n` becomes
    one newline character). Craft raw text just over 140 chars whose decoded
    value is under cap — the pre-fix bug (raw-length measurement via
    `unquote_yaml_scalar`, which its own docstring says does not process
    backslash escapes) would falsely truncate this and could slice mid-escape;
    the fix must leave it untouched."""
    # Decoded value: 135 'S' chars + one embedded newline = 136 decoded chars.
    # Raw quoted text: 135 'S' chars + literal backslash-n (2 raw chars) +
    # 2 surrounding quotes = 139 raw chars — under 140 raw, so this also
    # pins the "raw text longer than decoded" direction is not the only
    # divergence that matters; decoded (136) stays under cap too, so no
    # truncation should occur either way.
    decoded_body = "S" * 135
    content = _fm(f'summary: "{decoded_body}\\n"')

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is None
    assert capsys.readouterr().err == ""


def test_present_over_cap_summary_decoded_via_escape_still_truncates(capsys):
    """Decoded length over cap even though the backslash-escaped raw text is
    shorter than the decoded value would suggest at a glance — confirms
    truncation still fires correctly when measured off the DECODED value
    (150 decoded chars), not just skipped in the under-cap escape case above."""
    decoded_body = "S" * 149  # + 1 embedded newline == 150 decoded chars
    content = _fm(f'summary: "{decoded_body}\\n"')

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    summary = handoff_normalize.unquote_yaml_scalar(
        handoff_normalize.read_fm_field(fm_text, "summary")
    )
    assert len(summary) == 140
    assert summary.endswith("…")

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "150" in err  # original decoded length


def test_present_over_cap_summary_truncates_at_exactly_141_chars(capsys):
    """Boundary pin for the truncation arithmetic (code-reviewer P3): 141 is
    the tightest case for `value[:_SUMMARY_MAX_CHARS - 1] + '…'` — a slip to
    `_SUMMARY_MAX_CHARS` instead of `_SUMMARY_MAX_CHARS - 1` would only be
    caught at this exact boundary, not by the comfortably-clear 201 case."""
    over_cap = "S" * 141
    content = _fm(f"summary: {over_cap}")

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    summary = handoff_normalize.unquote_yaml_scalar(
        handoff_normalize.read_fm_field(fm_text, "summary")
    )
    assert len(summary) == 140
    assert summary.endswith("…")

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "141" in err


def test_absent_summary_still_backfills_from_h1_and_caps():
    content = _fm("summary: PLACEHOLDER").replace(
        "summary: PLACEHOLDER\n", ""
    ).replace(
        "# Some handoff\n\nBody.",
        "# " + ("A" * 200) + "\n\nBody.",
    )

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    summary = handoff_normalize.read_fm_field(fm_text, "summary")
    assert len(summary) == 140
    assert summary.endswith("…")
    assert any("summary: (absent)" in c for c in result["changes"])


def test_block_scalar_over_cap_summary_is_truncated_and_warns(capsys):
    over_cap = "S" * 201
    content = f"""\
---
title: Some handoff
created: 2026-08-13
pickup_ready: true
category: infra
summary: |-
  {over_cap}
deliverable_id: dlv-existing-123abc
initiative: null
owner: dbc-em-session-xyz
author: dbc-em-session-xyz
---

# Some handoff

Body.
"""

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    summary = handoff_normalize.unquote_yaml_scalar(
        handoff_normalize.read_fm_field(fm_text, "summary")
    )
    assert summary is not None
    assert len(summary) == 140
    assert summary.endswith("…")

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "block scalar" in err
    assert "201" in err


def test_block_scalar_at_cap_is_left_untouched(capsys):
    at_cap = "S" * 140
    content = f"""\
---
title: Some handoff
created: 2026-08-13
pickup_ready: true
category: infra
summary: |-
  {at_cap}
deliverable_id: dlv-existing-123abc
initiative: null
owner: dbc-em-session-xyz
author: dbc-em-session-xyz
---

# Some handoff

Body.
"""

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is None
    assert capsys.readouterr().err == ""


def test_block_scalar_indicator_with_trailing_comment_is_recognized(capsys):
    """Regression for the P3 finding (code-reviewer, 2026-08-13): a legal
    trailing comment on the indicator line itself (`summary: |-  # comment`)
    must still route to the block-scalar path, not fall through to the
    plain-scalar `else` branch and get measured/truncated as the literal
    indicator-plus-comment text."""
    over_cap = "S" * 201
    content = f"""\
---
title: Some handoff
created: 2026-08-13
pickup_ready: true
category: infra
summary: |-  # trailing comment on the indicator line
  {over_cap}
deliverable_id: dlv-existing-123abc
initiative: null
owner: dbc-em-session-xyz
author: dbc-em-session-xyz
---

# Some handoff

Body.
"""

    result = _normalize_one_text(content, _FILE_PATH)

    assert result is not None
    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    summary = handoff_normalize.unquote_yaml_scalar(
        handoff_normalize.read_fm_field(fm_text, "summary")
    )
    assert summary is not None
    assert len(summary) == 140
    assert summary.endswith("…")

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "block scalar" in err


def test_present_over_cap_summary_passes_validate_frontmatter_after_normalize():
    """A record authored with an over-cap summary is accepted by
    validate_frontmatter after normalization (AC-1/AC-3: the seam normalizes
    ahead of the still-strict gate)."""
    over_cap = "S" * 201
    content = _fm(f"summary: {over_cap}")

    result = _normalize_one_text(content, _FILE_PATH)
    assert result is not None

    fm_text = handoff_normalize.split_frontmatter(result["rebuilt"]).fm_text
    fm_dict = {
        "title": handoff_normalize.read_fm_field(fm_text, "title"),
        "created": handoff_normalize.read_fm_field(fm_text, "created"),
        "pickup_ready": True,
        "category": handoff_normalize.read_fm_field(fm_text, "category"),
        "summary": handoff_normalize.unquote_yaml_scalar(
            handoff_normalize.read_fm_field(fm_text, "summary")
        ),
        "deliverable_id": handoff_normalize.read_fm_field(fm_text, "deliverable_id"),
        "initiative": None,
        "owner": handoff_normalize.read_fm_field(fm_text, "owner"),
        "author": handoff_normalize.read_fm_field(fm_text, "author"),
        "kind": "install",
    }

    errors = validate_frontmatter(fm_dict, _SCHEMA_PATH)
    summary_errors = [e for e in errors if e.get("field") == "summary"]
    assert summary_errors == []


# ---------------------------------------------------------------------------
# Op-seam integration: a record authored through the birth seam passes
# validate_frontmatter after normalization (test-surface row for C1).
# ---------------------------------------------------------------------------


@pytest.mark.cadence
@pytest.mark.spawns_process
class TestAuthorForkSummaryCapEndToEnd:
    """`handoff_author_fork._handler` composes `_normalize_one_text` inline
    (an existing call site, not a new one — verified by reading the source
    before this fix). A title over 140 chars, with no H1 in the body, forces
    the backfill-from-title path through the now-capped normalization and the
    written record must still pass validate_frontmatter."""

    def test_long_title_backfills_a_capped_summary_that_validates(
        self, tmp_path, monkeypatch
    ):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-summary-cap")
        long_title = "T" * 200

        result = _run_fork(
            _fork_handler(
                {
                    "title": long_title,
                    "origin_plan_id": None,
                    "origin_goal_id": None,
                },
                repo_root=common_dir,
            )
        )

        assert result.get("status") == "ok", f"unexpected: {result}"
        content = Path(result["handoff_path"]).read_text(encoding="utf-8")
        split = handoff_normalize.split_frontmatter(content)
        summary = handoff_normalize.unquote_yaml_scalar(
            handoff_normalize.read_fm_field(split.fm_text, "summary")
        )
        assert summary is not None
        assert len(summary) <= 140

        fm_dict = {
            "title": handoff_normalize.read_fm_field(split.fm_text, "title"),
            "created": handoff_normalize.read_fm_field(split.fm_text, "created"),
            "summary": summary,
            "kind": handoff_normalize.read_fm_field(split.fm_text, "kind") or "install",
        }
        errors = validate_frontmatter(fm_dict, _SCHEMA_PATH)
        summary_errors = [e for e in errors if e.get("field") == "summary"]
        assert summary_errors == []


# ---------------------------------------------------------------------------
# Claim seam: a cosmetic over-cap `summary:` must not strand a baton.
#
# The claim verb of `handoff.transition` is the gate `pickup-assemble apply`
# reaches through `archive_stamp.cs_claim_handoff`. Before this fix it refused
# outright ("summary exceeds 140 characters (got 207)"), so a claim could not be
# taken until the operator hand-trimmed the field — the manual-editing loop the
# 2026-07-22 PM ruling (over-cap summary is cosmetic) exists to end. The claim
# verb now normalizes ahead of its own gate, exactly as `memo_transition` does.
# ---------------------------------------------------------------------------


def _claimable_record(summary_line: str) -> str:
    return f"""\
---
title: Some handoff
created: 2026-08-13
status: open
pickup_ready: true
category: infra
{summary_line}
deliverable_id: dlv-existing-123abc
initiative: null
owner: dbc-em-session-xyz
author: dbc-em-session-xyz
branch: work/summary-cap-claim
predecessor: null
---

# Some handoff

Body.
"""


@pytest.mark.cadence
@pytest.mark.spawns_process
class TestClaimSummaryCapNormalize:
    def _write(self, tmp_path, summary_line: str):
        repo_root = tmp_path / "repo"
        common_dir = _make_git_repo(repo_root)
        handoff = repo_root / "state" / "handoffs" / "x.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(_claimable_record(summary_line), encoding="utf-8")
        return common_dir, handoff

    def _claim(self, common_dir, handoff, verb: str = "claim"):
        return _run_fork(
            _transition_handler(
                {
                    "verb": verb,
                    "handoff_path": str(handoff),
                    "session_id": "sess-claim-summary-cap",
                    "at": "2026-08-14T10:00:00Z",
                },
                repo_root=common_dir,
            )
        )

    def _summary_on_disk(self, handoff) -> str:
        split = handoff_normalize.split_frontmatter(
            handoff.read_text(encoding="utf-8")
        )
        return handoff_normalize.unquote_yaml_scalar(
            handoff_normalize.read_fm_field(split.fm_text, "summary")
        )

    def test_over_cap_summary_no_longer_refuses_the_claim(self, tmp_path, capsys):
        """The live defect: a 207-char summary bounced `pickup-assemble apply`
        with `summary exceeds 140 characters`. It must now claim."""
        common_dir, handoff = self._write(tmp_path, "summary: " + "S" * 207)

        result = self._claim(common_dir, handoff)

        assert result.get("exit_code") == 0, f"unexpected: {result}"
        assert result.get("applied") is True
        summary = self._summary_on_disk(handoff)
        assert len(summary) == 140
        assert summary.endswith("…")  # truncation is visible, not silent-lossy
        assert "claimed" in handoff.read_text(encoding="utf-8")

        err = capsys.readouterr().err
        assert "handoff.transition: WARNING" in err
        assert "207" in err

    def test_block_scalar_over_cap_summary_also_claims(self, tmp_path):
        common_dir, handoff = self._write(
            tmp_path, "summary: |-\n  " + "S" * 207
        )

        result = self._claim(common_dir, handoff)

        assert result.get("exit_code") == 0, f"unexpected: {result}"
        summary = self._summary_on_disk(handoff)
        assert len(summary) == 140
        assert summary.endswith("…")

    def test_at_cap_summary_is_carried_through_the_claim_untouched(
        self, tmp_path, capsys
    ):
        at_cap = "S" * 140
        common_dir, handoff = self._write(tmp_path, f"summary: {at_cap}")

        result = self._claim(common_dir, handoff)

        assert result.get("exit_code") == 0, f"unexpected: {result}"
        assert self._summary_on_disk(handoff) == at_cap
        assert "summary" not in capsys.readouterr().err

    def test_non_summary_validation_failure_still_aborts_the_claim(self, tmp_path):
        """The gate is normalized-ahead-of, not relaxed: a rejection from any
        other cross-field rule still refuses the claim with the validator's own
        message (AC-7's no-rule-is-special-cased property, preserved)."""
        common_dir, handoff = self._write(tmp_path, "summary: fine")
        handoff.write_text(
            handoff.read_text(encoding="utf-8").replace(
                "category: infra", "category: not-a-real-category"
            ),
            encoding="utf-8",
        )

        result = self._claim(common_dir, handoff)

        assert result.get("exit_code") == 1
        assert "validation failed" in result.get("error", "")
        assert "status: open" in handoff.read_text(encoding="utf-8")
