"""Plan-row coding helpers: the line-level open->coded flip, chunk-to-source-row mapping, and the outside-landing detail text; the real-commit cases live in test_plan_row_coding_spawns."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit.plan_row_coding import (
    NOOP_DETAIL,
    advance_to_executing,
    flip_rows_coded,
    inventory_record_rel,
    outside_landing_detail,
    source_rows_by_plan,
)

_SHA = "a" * 40
_REL = "plan.md"
_ROWS = "".join(
    f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n  disposition: open\n  deferred: false\n"
    for i in ("C1", "C2")
)
_PLAN = f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{_ROWS}```\n"


def _rows(text: str) -> dict:
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(text).body)}


def test_flip_stamps_noop_rows_with_the_no_change_detail():
    text, flipped = flip_rows_coded(_PLAN, {"C1", "C2"}, _SHA, {"C2": NOOP_DETAIL})
    rows = _rows(text)
    assert flipped == ["C1", "C2"]
    assert rows["C2"]["disposition"] == "coded"
    assert rows["C2"]["disposition_ref"] == _SHA
    assert rows["C2"]["disposition_detail"] == NOOP_DETAIL
    assert "disposition_detail" not in rows["C1"]


def test_flip_leaves_a_row_that_is_not_open_alone():
    coded, _ = flip_rows_coded(_PLAN, {"C1"}, _SHA)
    again, flipped = flip_rows_coded(coded, {"C1"}, "b" * 40)
    assert flipped == [] and again == coded


def test_flip_keeps_a_crlf_plan_crlf():
    crlf = _PLAN.replace("\n", "\r\n")
    text, flipped = flip_rows_coded(crlf, {"C1"}, _SHA)
    assert flipped == ["C1"] and "\r\n" in text and "\n" not in text.replace("\r\n", "")


def _with_status(status: str) -> str:
    return _PLAN.replace("title: p\n", f"title: p\nstatus: {status}\n", 1)


@pytest.mark.parametrize("status", ["draft", "reviewed", "approved"])
def test_a_flipped_row_advances_a_pre_executing_plan(status):
    text, flipped = flip_rows_coded(_with_status(status), {"C1"}, _SHA)
    assert flipped == ["C1"] and "status: executing\n" in text and f"status: {status}" not in text


@pytest.mark.parametrize("status", ["executing", "landed", "implemented", "blocked", "superseded"])
def test_a_flipped_row_leaves_other_statuses_byte_identical(status):
    plan = _with_status(status)
    text, _ = flip_rows_coded(plan, {"C1"}, _SHA)
    assert f"status: {status}\n" in text
    assert advance_to_executing(plan) is plan


def test_no_flipped_row_leaves_the_status_alone():
    coded, _ = flip_rows_coded(_with_status("executing"), {"C1", "C2"}, _SHA)
    plan = coded.replace("status: executing", "status: reviewed")
    again, flipped = flip_rows_coded(plan, {"C1", "C2"}, _SHA)
    assert flipped == [] and again is plan


def test_a_crlf_reviewed_plan_stays_crlf_and_reads_executing():
    text, flipped = flip_rows_coded(_with_status("reviewed").replace("\n", "\r\n"), {"C1"}, _SHA)
    assert flipped == ["C1"] and "status: executing\r\n" in text and "\n" not in text.replace("\r\n", "")


def test_advance_without_frontmatter_returns_the_input():
    assert advance_to_executing("no frontmatter\n") == "no frontmatter\n"


def test_a_plan_mode_marker_maps_its_chunk_ids_to_itself(tmp_path):
    (tmp_path / _REL).write_text(_PLAN, encoding="utf-8")
    assert source_rows_by_plan(tmp_path, _REL, ["C1", "C2"]) == {_REL: {"C1", "C2"}}
    assert source_rows_by_plan(tmp_path, _REL, []) == {}
    assert source_rows_by_plan(tmp_path, None, ["C1"]) == {}
    assert source_rows_by_plan(tmp_path, "missing.md", ["C1"]) == {}


def test_inventory_record_rel_is_none_for_a_path_that_is_not_a_spine(tmp_path):
    assert inventory_record_rel(tmp_path, "docs/plans/p.md") is None


def test_outside_landing_detail_names_the_short_shas():
    text = outside_landing_detail(["a" * 40, "b" * 40])
    assert "aaaaaaaaaa, bbbbbbbbbb" in text and text.startswith("resolved as a no-op")
