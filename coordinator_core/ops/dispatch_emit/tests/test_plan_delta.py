import json

from coordinator_core.ops.dispatch_emit.plan_delta import classify, scope_projection

FM = """---
title: T
status: planning
register_claims:
  rows: ["R1"]
census: []
prime_exit_criterion:
  statement: S
  falsifier:
    how: "pytest a"
    baseline_output: "x"
    expected_when_true: "ok"
---
"""

ROW_C1 = """- id: C1
  title: one
  change_kind: script-edit
  writes:
    - a/b.py
  reads_at_head:
    - r.py
  register_rows: ["R1"]
  acceptance: ok
"""


def plan(fm=FM, rows=ROW_C1, prose="## Anti-scope\n\nnone\n"):
    return f"{fm}\n# T\n\n{prose}\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


BASE = plan()


def test_projection_is_json_able_and_unchanged_on_identical_text():
    proj = scope_projection(BASE)
    assert json.loads(json.dumps(proj)) == proj
    assert classify(proj, BASE) == "unchanged"


def test_unparsable_plan_projects_none_and_never_raises():
    assert scope_projection("no frontmatter, no spine") is None
    assert scope_projection(plan(fm="---\n: [bad\n---\n")) is None
    assert scope_projection(plan(rows="- id: [unterminated\n")) is None


def test_crlf_and_backslash_paths_do_not_change_the_projection():
    proj = scope_projection(BASE)
    assert classify(proj, BASE.replace("\n", "\r\n")) == "unchanged"
    assert classify(proj, BASE.replace("a/b.py", "a\\b.py")) == "unchanged"


def test_repair_reads_at_head_register_row_and_lifecycle_status():
    proj = scope_projection(BASE)
    assert classify(proj, plan(rows=ROW_C1.replace("r.py", "other.py"))) == "repair"
    assert classify(proj, plan(rows=ROW_C1.replace('["R1"]', '["R1", "R2"]'))) == "repair"
    assert classify(proj, plan(fm=FM.replace('rows: ["R1"]', 'rows: ["R1", "R2"]'))) == "repair"
    assert classify(proj, plan(fm=FM.replace("planning", "executing"))) == "unchanged"


def test_repair_added_test_row_and_falsifier_how_change():
    proj = scope_projection(BASE)
    test_row = "- id: C2\n  title: t\n  writes:\n    - pkg/tests/test_x.py\n"
    assert classify(proj, plan(rows=ROW_C1 + test_row)) == "repair"
    assert classify(proj, plan(fm=FM.replace("pytest a", "pytest b").replace('"x"', '"y"'))) == "repair"


def test_repair_falsifier_added_where_none_existed():
    no_f = FM.split("  falsifier:")[0] + "---\n"
    proj = scope_projection(plan(fm=no_f))
    assert classify(proj, BASE) == "repair"
    assert classify(scope_projection(BASE), plan(fm=no_f)) == "scope"


def test_scope_changed_writes_title_acceptance_and_prose():
    proj = scope_projection(BASE)
    assert classify(proj, plan(rows=ROW_C1.replace("a/b.py", "a/c.py"))) == "scope"
    assert classify(proj, plan(rows=ROW_C1.replace("one", "two"))) == "scope"
    assert classify(proj, plan(rows=ROW_C1.replace("acceptance: ok", "acceptance: no"))) == "scope"
    assert classify(proj, plan(prose="## Anti-scope\n\nmore\n")) == "scope"
    assert classify(proj, plan(fm=FM.replace("statement: S", "statement: Z"))) == "scope"
    assert classify(proj, plan(fm=FM.replace("ok", "different"))) == "scope"


def test_scope_removed_row_added_non_test_row_and_mixed_writes_row():
    two = ROW_C1 + "- id: C2\n  title: t\n  writes:\n    - pkg/tests/test_x.py\n"
    assert classify(scope_projection(two), BASE) == "scope"
    assert classify(scope_projection(BASE), plan(rows=ROW_C1 + "- id: C2\n  writes:\n    - pkg/m.py\n")) == "scope"
    mixed = "- id: C2\n  writes:\n    - pkg/tests/test_x.py\n    - pkg/m.py\n"
    assert classify(scope_projection(BASE), plan(rows=ROW_C1 + mixed)) == "scope"
    undeclared = "- id: C2\n  title: t\n"
    assert classify(scope_projection(BASE), plan(rows=ROW_C1 + undeclared)) == "scope"


def test_scope_for_unparsable_current_or_foreign_prior():
    proj = scope_projection(BASE)
    assert classify(proj, "garbage") == "scope"
    assert classify(None, BASE) == "scope"
    assert classify({**proj, "v": 0}, BASE) == "scope"
    assert classify({"v": 1}, BASE) == "scope"
