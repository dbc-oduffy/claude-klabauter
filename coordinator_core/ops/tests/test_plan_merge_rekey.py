"""plan.merge collision detection and re-key: the refusal, the splice, and the untouched paths."""

from coordinator_core.ops._plan_merge_contract import Refusal, SourceRow
from coordinator_core.ops._plan_merge_rekey import apply_rekey, detect_collisions

A, B = "docs/plans/a.md", "docs/plans/b.md"


def _row(src, rid, deps=(), extra="  title: \"t: C1 stays\"\n"):
    dep = ""
    if deps:
        dep = "  depends_on:\n" + "".join(f"    - chunk: {d}\n      gate_kind: x\n" for d in deps)
    return SourceRow(src, rid, f"- id: {rid}\n{extra}{dep}  body: |\n    mentions chunk: {rid}\n")


def _colliding():
    return [_row(A, "C1"), _row(A, "C2", ["C1"]), _row(B, "C1"), _row(B, "C3", ["C1"])]


def test_collisions_name_id_and_both_paths():
    assert detect_collisions(_colliding()) == [("C1", (A, B))]


def test_disjoint_ids_pass_unchanged():
    rows = [_row(A, "M1"), _row(A, "M2", ["M1"]), _row(B, "S1")]
    assert detect_collisions(rows) == []
    assert apply_rekey(rows, {A: {"M1": "M1"}}) == rows
    assert apply_rekey(rows, None) == rows


def test_rekey_rewrites_id_and_same_source_edges_only():
    out = apply_rekey(_colliding(), {B: {"C1": "B1"}})
    assert not isinstance(out, Refusal)
    assert [r.row_id for r in out] == ["C1", "C2", "B1", "C3"]
    assert out[0] == _colliding()[0] and out[1] == _colliding()[1]
    assert out[2].text.startswith("- id: B1\n")
    assert "- chunk: B1\n" in out[3].text and "- chunk: C1\n" not in out[3].text
    assert "mentions chunk: C1" in out[2].text and 'title: "t: C1 stays"' in out[2].text
    assert detect_collisions(out) == []


def test_rekey_is_simultaneous_for_a_swap():
    rows = [_row(A, "X", ["Y"]), _row(A, "Y")]
    out = apply_rekey(rows, {A: {"X": "Y", "Y": "X"}})
    assert [r.row_id for r in out] == ["Y", "X"]
    assert "- chunk: X\n" in out[0].text


def test_mapping_to_absent_id_or_source_refuses():
    for rk in ({A: {"NOPE": "Z"}}, {"docs/plans/c.md": {"C1": "Z"}}):
        r = apply_rekey(_colliding(), rk)
        assert isinstance(r, Refusal) and r.code == "REKEY_DANGLING"


def test_rekey_that_still_collides_refuses():
    r = apply_rekey(_colliding(), {B: {"C3": "C2"}, A: {"C2": "C2"}})
    assert isinstance(r, Refusal) and r.code == "REKEY_DANGLING"
    assert r.sources == (A, B) and "C1" in r.row_ids
