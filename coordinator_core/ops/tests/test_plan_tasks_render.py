"""
coordinator_core.ops.tests.test_plan_tasks_render

Tests for coordinator_core.ops.plan_tasks_render — the read/projection
module over the plan `## Tasks` task-spine's disposition field set.

Coverage (mapped to
docs/plans/2026-07-27-plan-line-item-resolution-model.md § C9, AC15):
  - load_rows: LOCATED / ABSENT / MALFORMED pass-through from
    body_blocks.locate_fenced_block, plus non-list-body and
    non-dict-entry degrade-to-MALFORMED.
  - spine_projection: open rows returned in full, closed_count covers
    every non-open disposition including coded (D5's full partition).
  - render_closed_items: grouped by disposition in fixed order
    (spun_off, backlogged, wont_do), coded/open excluded, empty when
    nothing closed, ref/detail rendered per row.

Spec backlink: coordinator_core/ops/plan_tasks_render.py
Plan: docs/plans/2026-07-27-plan-line-item-resolution-model.md § C9
"""

from __future__ import annotations

import textwrap

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.plan_tasks_render import (
    load_rows,
    render_closed_items,
    spine_projection,
)


# ---------------------------------------------------------------------------
# load_rows
# ---------------------------------------------------------------------------


def _plan_source(fence_body: str) -> str:
    # Deliberately NOT built via textwrap.dedent — the fence body is
    # interpolated at column 0 (locate_fenced_block's regexes require
    # unindented `## Tasks` and ```` ```yaml plan-tasks ```` lines), while a
    # single dedent() call over the whole f-string would see mixed
    # indentation between the template lines and the interpolated body and
    # no-op, leaving the template lines indented and unmatchable.
    return (
        '---\ntitle: "Test plan"\n---\n\n'
        "# Test plan\n\n"
        "## Tasks\n\n"
        "```yaml plan-tasks\n"
        f"{fence_body}\n"
        "```\n"
    )


def test_load_rows_located():
    source = _plan_source(
        textwrap.dedent(
            """\
            - id: C1
              title: First
              change_kind: code-edit
              surface: foo.py
              disposition: open
            """
        ).rstrip("\n")
    )
    result = load_rows(source)
    assert result.status is LocateStatus.LOCATED
    assert len(result.rows) == 1
    assert result.rows[0]["id"] == "C1"


def test_load_rows_absent_when_no_heading():
    source = "---\ntitle: t\n---\n\n# Test plan\n\nNo tasks heading here.\n"
    result = load_rows(source)
    assert result.status is LocateStatus.ABSENT
    assert result.rows == []


def test_load_rows_malformed_when_two_fences():
    body = "- id: C1\n  title: t\n  change_kind: code-edit\n  surface: x\n"
    fence = f"```yaml plan-tasks\n{body}```\n"
    source = "---\ntitle: t\n---\n\n## Tasks\n\n" + fence + "\n" + fence
    result = load_rows(source)
    assert result.status is LocateStatus.MALFORMED
    assert result.rows == []


def test_load_rows_malformed_when_body_not_a_list():
    source = _plan_source("not_a_list: true")
    result = load_rows(source)
    assert result.status is LocateStatus.MALFORMED
    assert result.rows == []


def test_load_rows_malformed_when_entry_is_not_a_dict():
    source = _plan_source("- just_a_string\n- id: C1\n  title: t")
    result = load_rows(source)
    assert result.status is LocateStatus.MALFORMED
    assert result.rows == []


# ---------------------------------------------------------------------------
# spine_projection
# ---------------------------------------------------------------------------


def test_spine_projection_open_rows_in_full():
    rows = [
        {"id": "C1", "title": "One", "disposition": "open"},
        {"id": "C2", "title": "Two"},  # missing disposition -> defaults open
    ]
    projection = spine_projection(rows)
    assert projection["open"] == rows
    assert projection["closed_count"] == 0


def test_spine_projection_closed_count_includes_coded():
    rows = [
        {"id": "C1", "title": "One", "disposition": "open"},
        {"id": "C2", "title": "Two", "disposition": "coded", "disposition_ref": "abc1234"},
        {"id": "C3", "title": "Three", "disposition": "spun_off", "disposition_ref": "docs/plans/x.md"},
        {"id": "C4", "title": "Four", "disposition": "backlogged", "disposition_ref": "state/improvement-queue/x.yaml"},
        {"id": "C5", "title": "Five", "disposition": "wont_do", "disposition_detail": "not worth it"},
    ]
    projection = spine_projection(rows)
    assert [row["id"] for row in projection["open"]] == ["C1"]
    assert projection["closed_count"] == 4


def test_spine_projection_empty_rows():
    assert spine_projection([]) == {"open": [], "closed_count": 0}


# ---------------------------------------------------------------------------
# render_closed_items
# ---------------------------------------------------------------------------


def test_render_closed_items_empty_when_nothing_closed():
    rows = [
        {"id": "C1", "title": "One", "disposition": "open"},
        {"id": "C2", "title": "Two", "disposition": "coded", "disposition_ref": "abc1234"},
    ]
    assert render_closed_items(rows) == ""


def test_render_closed_items_empty_when_no_rows():
    assert render_closed_items([]) == ""


def test_render_closed_items_excludes_coded_and_open():
    rows = [
        {"id": "C1", "title": "Open one", "disposition": "open"},
        {"id": "C2", "title": "Coded one", "disposition": "coded", "disposition_ref": "abc1234"},
        {"id": "C3", "title": "Spun off one", "disposition": "spun_off", "disposition_ref": "docs/plans/x.md"},
    ]
    rendered = render_closed_items(rows)
    assert "C1" not in rendered
    assert "C2" not in rendered
    assert "C3" in rendered


def test_render_closed_items_grouped_in_fixed_order():
    rows = [
        {"id": "C1", "title": "Won't do", "disposition": "wont_do", "disposition_detail": "declined"},
        {"id": "C2", "title": "Backlogged", "disposition": "backlogged", "disposition_ref": "state/improvement-queue/x.yaml"},
        {"id": "C3", "title": "Spun off", "disposition": "spun_off", "disposition_ref": "docs/plans/y.md"},
    ]
    rendered = render_closed_items(rows)
    spun_off_idx = rendered.index("### Spun off")
    backlogged_idx = rendered.index("### Backlogged")
    wont_do_idx = rendered.index("### Won't do")
    assert spun_off_idx < backlogged_idx < wont_do_idx


def test_render_closed_items_includes_ref_and_detail():
    rows = [
        {
            "id": "C4",
            "title": "Deferred thing",
            "disposition": "backlogged",
            "disposition_ref": "state/improvement-queue/2026-07-27-thing.yaml",
            "disposition_detail": "PM ratified 2026-07-27",
        },
    ]
    rendered = render_closed_items(rows)
    assert "C4" in rendered
    assert "Deferred thing" in rendered
    assert "state/improvement-queue/2026-07-27-thing.yaml" in rendered
    assert "PM ratified 2026-07-27" in rendered


def test_render_closed_items_wont_do_has_no_ref():
    rows = [
        {"id": "C5", "title": "Declined thing", "disposition": "wont_do", "disposition_detail": "not worth it"},
    ]
    rendered = render_closed_items(rows)
    assert "C5" in rendered
    assert "not worth it" in rendered


def test_render_closed_items_ends_with_trailing_newline():
    rows = [
        {"id": "C1", "title": "One", "disposition": "wont_do", "disposition_detail": "nope"},
    ]
    rendered = render_closed_items(rows)
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")
