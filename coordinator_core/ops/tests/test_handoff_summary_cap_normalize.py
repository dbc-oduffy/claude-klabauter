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
