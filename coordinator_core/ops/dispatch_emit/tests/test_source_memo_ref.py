"""source_memo_ref: inbox memo path extraction and plan-then-sizing resolution."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.source_memo_ref import (
    inbox_memo_refs,
    resolve_source_memo,
    sizing_source_memo,
)

X = "state/cross-repo/inbox/x.md"
Y = "state/cross-repo/inbox/y.md"


def _sizing(*texts):
    return {"requirement_register": {"rows": [{"id": f"R{i}", "source_text": t} for i, t in enumerate(texts)]}}


def test_one_path_found():
    assert inbox_memo_refs(f"please close {X} on landing.") == (X,)
    assert inbox_memo_refs(f"`{X}`") == (X,)


def test_backslash_spelling_normalised():
    assert inbox_memo_refs(r"state\cross-repo\inbox\x.md") == (X,)


def test_placeholder_not_matched():
    assert inbox_memo_refs("state/cross-repo/inbox/<memo>.md") == ()


def test_prefixed_and_absolute_not_matched():
    assert inbox_memo_refs("coordinator-content-repo/" + X) == ()
    assert inbox_memo_refs("/abs/root/" + X) == ()
    assert inbox_memo_refs("C:\\repos\\other\\state\\cross-repo\\inbox\\x.md") == ()  # abs-path-ok: negative fixture


def test_dotdot_refused():
    assert inbox_memo_refs("state/cross-repo/inbox/..md") == ()
    assert inbox_memo_refs("state/cross-repo/inbox/a..b.md") == ()
    assert inbox_memo_refs("state/cross-repo/inbox/../x.md") == ()


def test_first_seen_order_and_dedupe():
    assert inbox_memo_refs(f"{Y} then {X} then {Y}") == (Y, X)


def test_sizing_two_distinct_none_duplicate_one():
    assert sizing_source_memo(_sizing(f"{X} and {Y}")) is None
    assert sizing_source_memo(_sizing(X, f"again {X}")) == X
    assert sizing_source_memo({"intent": f"close {X}", "pm_verbatims": [{"text": X}]}) == X
    assert sizing_source_memo(_sizing("nothing")) is None


def _write_plan(root, fm_extra):
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text(f"---\ntitle: p\n{fm_extra}---\n\nbody\n", encoding="utf-8")


def _write_sizing(root, text):
    (root / "state" / "sizings" / "s.yaml").write_text(
        "requirement_register:\n  rows:\n  - id: R1\n    source_text: '" + text + "'\n", encoding="utf-8"
    )


def test_resolve_prefers_plan_frontmatter(tmp_path):
    _write_plan(tmp_path, f"source_memo: {Y}\nsizing_object: state/sizings/s.yaml\n")
    _write_sizing(tmp_path, X)
    assert resolve_source_memo(tmp_path, "plan.md") == Y


def test_resolve_falls_back_to_sizing(tmp_path):
    _write_plan(tmp_path, "source_memo: null\nsizing_object: state/sizings/s.yaml\n")
    _write_sizing(tmp_path, f"close {X}")
    assert resolve_source_memo(tmp_path, "plan.md") == X


def test_resolve_missing_plan_or_sizing(tmp_path):
    assert resolve_source_memo(tmp_path, "nope.md") is None
    assert resolve_source_memo(tmp_path, None) is None
    _write_plan(tmp_path, "sizing_object: state/sizings/gone.yaml\n")
    assert resolve_source_memo(tmp_path, "plan.md") is None
