"""`approved_body_sha` ignores the `depends_on_plan` edges `plan.seam_fix` appends
to spine rows, and still refuses a prose or real row edit."""

from __future__ import annotations

from coordinator_core.frontmatter.primitives import (
    APPROVED_BODY_CHANGED,
    APPROVED_BODY_OK,
    approval_body_sha,
    check_approved_body,
    stamp_approved_body_sha,
)

_ROWS = """- id: C1
  title: t1
  change_kind: code-edit
  surface: a.py
  deferred: false
  body: |
    depends_on_plan: mentioned in prose
- id: C2
  title: t2
  change_kind: code-edit
  surface: a.py
  deferred: false
"""


def _plan(rows: str = _ROWS, prose: str = "Prose.") -> str:
    return (
        "---\ntitle: p\nstatus: approved\n---\n\n# Plan\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{rows.rstrip()}\n```\n\n{prose}\n"
    )


def _approved() -> str:
    return stamp_approved_body_sha(_plan())


def test_block_list_edge_append_keeps_stamp_valid():
    approved = _approved()
    edited = approved.replace(
        "  deferred: false\n- id: C2",
        "  deferred: false\n  depends_on_plan:\n  - docs/plans/other.md\n- id: C2",
    ).replace(
        "  deferred: false\n```",
        "  deferred: false\n  depends_on_plan:\n  - docs/plans/a.md\n  - docs/plans/b.md\n```",
    )
    assert edited != approved
    assert check_approved_body(edited) == (APPROVED_BODY_OK, "")


def test_flow_list_edge_append_keeps_stamp_valid():
    approved = _approved()
    edited = approved.replace(
        "  deferred: false\n```", "  deferred: false\n  depends_on_plan: [docs/plans/a.md]\n```"
    )
    assert edited != approved
    assert check_approved_body(edited)[0] == APPROVED_BODY_OK


def test_prose_edit_after_edge_append_is_changed():
    edited = _approved().replace(
        "  deferred: false\n```", "  deferred: false\n  depends_on_plan: [docs/plans/a.md]\n```"
    )
    assert check_approved_body(edited)[0] == APPROVED_BODY_OK
    state, msg = check_approved_body(edited.replace("Prose.", "Edited prose."))
    assert state == APPROVED_BODY_CHANGED
    assert "plan body changed since approval" in msg


def test_real_row_edit_still_changes_the_hash():
    approved = _approved()
    assert check_approved_body(approved.replace("title: t2", "title: other"))[0] == APPROVED_BODY_CHANGED


def test_block_scalar_text_naming_the_key_still_counts():
    approved = _approved()
    edited = approved.replace("mentioned in prose", "mentioned elsewhere")
    assert check_approved_body(edited)[0] == APPROVED_BODY_CHANGED


def test_pre_edge_neutralising_stamp_still_accepted():
    plan = _plan()
    legacy = plan.replace(
        "status: approved", f"status: approved\napproved_body_sha: {approval_body_sha(plan, neutral_plan_edges=False)}"
    )
    edited = legacy.replace(
        "  deferred: false\n```", "  deferred: false\n  depends_on_plan: [docs/plans/a.md]\n```"
    )
    assert check_approved_body(legacy)[0] == APPROVED_BODY_OK
    assert check_approved_body(edited)[0] == APPROVED_BODY_OK
