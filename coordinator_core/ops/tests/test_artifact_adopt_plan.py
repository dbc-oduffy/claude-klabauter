from __future__ import annotations

from coordinator_core.ops.artifact_adopt_plan import PlanDerived, port_plan_frontmatter

ENUM = ["draft", "reviewed", "approved", "executing", "closed_partial"]
DERIVED: PlanDerived = {
    "created": "2026-10-01",
    "author": "Ada (sess-1)",
    "branch": "work/x",
    "plan_id": "pln-t-abc123",
    "deliverable_id": "dlv-t-def456",
}


def _port(fm, body="", derived=DERIVED):
    return port_plan_frontmatter(fm, body, derived, ENUM)


def test_alias_renames_only_when_target_absent():
    out, ch = _port("date: 2026-01-02\nauthors: Bob\nstate: draft\nname: T\n")
    assert "created: 2026-01-02" in out and "author: Bob" in out
    assert "status: draft" in out and "title: T" in out
    assert "date:" not in out and "name:" not in out
    out, _ = _port("date: 2026-01-02\ncreated: 2025-05-05\n")
    assert "date: 2026-01-02" in out and "created: 2025-05-05" in out


def test_present_plan_id_untouched():
    out, _ = _port("title: T\nplan_id: pln-old-000000\n")
    assert "plan_id: pln-old-000000" in out and "pln-t-abc123" not in out


def test_second_call_is_noop():
    out, ch = _port("title: T\nstatus: Draft\n")
    assert ch
    out2, ch2 = _port(out)
    assert ch2 == [] and out2 == out


def test_comments_and_order_survive():
    fm = "title: T  # keep\n# note\nstatus: draft\n"
    out, _ = _port(fm)
    assert out.startswith(fm)
    assert out.index("created:") < out.index("author:") < out.index("branch:")


def test_no_frontmatter_takes_h1_title():
    out, ch = _port(None, "intro\n# My Plan\n\ntext\n")
    assert out.startswith('title: "My Plan"\n')
    assert "created: 2026-10-01" in out and "initiative: null" in out
    assert "status" not in out


def test_unknown_keys_kept():
    out, _ = _port("title: T\nstatus: draft\ncloses: x\nkind: y\n")
    assert "closes: x" in out and "kind: y" in out


def test_status_case_fold_only_when_valid():
    out, ch = _port("title: T\nstatus: Approved\n")
    assert "status: approved" in out
    out, _ = _port("title: T\nstatus: bogus\n")
    assert "status: bogus" in out


def test_unsupplied_values_not_filled():
    out, _ = _port("title: T\n", derived={})
    assert "created" not in out and "plan_id" not in out
