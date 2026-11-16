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


def _spine_of(tmp_path, row_extra: str):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan = tmp_path / "docs" / "plans" / "2026-11-12-fixture.md"
    spine = (
        "- id: T1\n"
        "  title: Send the memo\n"
        "  body: Send the memo to the receiver and confirm the receipt.\n"
        "  change_kind: memo-send\n"
        "  surface: docs/x.md\n"
        "  writes: []\n"
        "  queue_scope: project\n"
        "  disposition: open\n"
        "  kind: memo-send\n"
        "  memo_to: coordinator-content-repo-em\n" + row_extra
    )
    text = (
        "---\ntitle: f\nstatus: draft\ncreated: 2026-11-12\nauthor: f\n---\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{spine}```\n"
    )
    plan.write_text(text, encoding="utf-8")
    return pg._spine(plan, text, tmp_path)


def test_slugless_open_memo_send_row_is_refused_naming_the_row(tmp_path):
    result = _spine_of(tmp_path, "")
    assert result["status"] == "DEFECT"
    assert result["kind"] == "memo-send-slugless"
    assert "T1" in result["detail"]


def test_slugged_memo_send_row_passes_the_slug_check(tmp_path):
    result = _spine_of(tmp_path, "  receipt: slug-a\n")
    assert result.get("kind") != "memo-send-slugless"


def test_performer_em_slugless_memo_send_row_passes_the_slug_check(tmp_path):
    result = _spine_of(tmp_path, "  performer: em\n")
    assert result.get("kind") != "memo-send-slugless"


def test_memo_row_naming_the_send_verb_does_not_trip_executor_commit():
    row = {**_MEMO, "body": "Run `cross-repo-memo send slug-a` then confirm the receipt."}
    assert pg._executor_commit([row])["status"] == "PASS"
