"""prep_gate reads a dispatchable memo-send row's plan edges; memo rows still skip the commit leg."""

from __future__ import annotations

from coordinator_core.roadmap import prep_gate as pg

_EDGE = [{"plan": "docs/plans/2026-09-07-gone.md", "chunk": "C1"}]
_MEMO = {
    "id": "M1",
    "kind": "memo-send",
    "receipt": "slug-a",
    "memo_to": "coordinator-content-repo-em",
    "depends_on_plan": _EDGE,
}


def test_dispatchable_memo_row_dangling_edge_is_reported(tmp_path):
    (tmp_path / ".git").mkdir()
    _, _, dangling = pg._sibling_plan_edges(dict(_MEMO), "M1", tmp_path, {})
    assert dangling


def test_non_dispatchable_memo_row_edges_stay_unread(tmp_path):
    (tmp_path / ".git").mkdir()
    row = {**_MEMO, "performer": "em"}
    assert pg._sibling_plan_edges(row, "M1", tmp_path, {}) == (frozenset(), [], [])


def test_memo_row_naming_the_send_verb_does_not_trip_executor_commit():
    row = {**_MEMO, "body": "Run `cross-repo-memo send slug-a` then confirm the receipt."}
    assert pg._executor_commit([row])["status"] == "PASS"
