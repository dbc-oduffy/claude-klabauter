"""The plan-reconciliation leg names the sibling run a still-open spine row waits on, and the refire call."""

from __future__ import annotations

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.ceremony.tests.test_close_background_land_run_legs import (
    PLAN,
    _build,
    _fake_mint,
    _flip_ctx,
)

ENTRY = {
    "row": "C2",
    "run_id": "sib-0772b0",
    "label": "module-axis",
    "status": "running",
    "plan": "docs/plans/other.md",
    "via": "depends_on_plan",
    "refire": 'Workflow({name: "w", args: {sizing: "s.yaml"}})',
}


def _detail(tmp_path, **record) -> str:
    root = tmp_path / "repo"
    sha = _build(root, spine=True)
    _fake_mint(root / PLAN, root)
    result = cb._plan_flip(_flip_ctx(root, sha, **record))
    assert result.status == "failed" and "spine row(s) still open" in result.detail
    return result.detail


def test_open_row_with_a_sibling_entry_names_the_run_status_and_refire(tmp_path):
    detail = _detail(tmp_path, inputs={"sibling_blocked": [ENTRY]})
    assert f"C2 (waits on sibling run sib-0772b0, running; after it lands: {ENTRY['refire']})" in detail


def test_text_is_bare_without_the_key(tmp_path):
    detail = _detail(tmp_path)
    assert "waits on sibling" not in detail
    assert detail.endswith("C2") or ", C2" in detail or "open: C2" in detail


def test_entry_for_a_row_that_is_not_open_adds_nothing(tmp_path):
    other = dict(ENTRY, row="C9")
    assert "waits on sibling" not in _detail(tmp_path, inputs={"sibling_blocked": [other]})


def test_wait_notes_skip_malformed_entries_and_keep_the_first_per_row():
    notes = cb._sibling_wait_notes(["x", {"row": "C1"}, ENTRY, dict(ENTRY, run_id="later")])
    assert list(notes) == ["C2"] and "sib-0772b0" in notes["C2"]
    assert cb._sibling_wait_notes(None) == {}
