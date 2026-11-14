"""reverify_export_split and reverify_red_findings(fresh=): the isolated-export record decides foreign reds."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

LANDED = "pkg/mod.py"
TEST = "pkg/tests/test_mod.py"
A, B, C, D = (f"{TEST}::test_{x}" for x in "abcd")


def _fresh(**baseline) -> dict:
    return {"baseline": baseline}


def _tree(root: Path) -> None:
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / LANDED).write_text("X = 1\n", encoding="utf-8")
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / TEST).write_text("from pkg import mod\n", encoding="utf-8")


def _params(failing) -> dict:
    return {"inline_review": {ltt.REVERIFY_KEY: {"status_before": "fail", "status_after": "fail", "failing": list(failing)}}}


def test_split_three_ways_and_head_red_outranks_export_red():
    fresh = _fresh(head_red=[A, D], owed_cleared=[B], reverify_export_red=[C, D])
    owned, foreign, unmeasured = ltt.reverify_export_split(fresh, [A, B, C, D, "x::e"])
    assert owned == {C}
    assert foreign == {A, B, D}
    assert unmeasured == {"x::e"}


def test_record_without_export_red_key_leaves_every_unlisted_id_unmeasured():
    fresh = _fresh(head_red=[A])
    assert ltt.reverify_export_split(fresh, [A, B]) == (set(), {A}, {B})
    assert ltt.reverify_export_split(None, [A]) == (set(), set(), {A})
    assert ltt.reverify_export_split({}, [A]) == (set(), set(), {A})


def test_ids_compare_posix_normalised_and_keep_caller_spelling():
    back = TEST.replace("/", "\\") + "::test_a"
    owned, foreign, unmeasured = ltt.reverify_export_split(_fresh(owed_cleared=[A]), [back])
    assert foreign == {back} and not owned and not unmeasured


def test_default_keeps_today_behaviour_and_fresh_drops_foreign_ids(tmp_path):
    _tree(tmp_path)
    p = _params([A, B])
    assert len(ltt.reverify_red_findings(tmp_path, p, [LANDED])) == 1
    fresh = _fresh(owed_cleared=[A, B], reverify_export_red=[])
    assert ltt.reverify_red_findings(tmp_path, p, [LANDED], fresh=fresh) == []


def test_owned_or_unmeasured_id_still_refuses_with_fresh(tmp_path):
    _tree(tmp_path)
    p = _params([A, B])
    owned = _fresh(owed_cleared=[A], reverify_export_red=[B])
    found = ltt.reverify_red_findings(tmp_path, p, [LANDED], fresh=owned)
    assert len(found) == 1 and B in found[0].detail and A not in found[0].detail
    unmeasured = _fresh(owed_cleared=[A])
    found = ltt.reverify_red_findings(tmp_path, p, [LANDED], fresh=unmeasured)
    assert len(found) == 1 and B in found[0].detail


def test_red_naming_no_test_still_refuses_with_fresh(tmp_path):
    _tree(tmp_path)
    found = ltt.reverify_red_findings(tmp_path, _params([]), [LANDED], fresh=_fresh(head_red=[A]))
    assert len(found) == 1 and "naming no failing test" in found[0].detail


def test_reverify_live_failing_normalises_dedupes_and_drops_base_red():
    record = {
        "failing": ["./a\\t.py::x", "a/t.py::x", "a/t.py::y", "a/t.py::z"],
        "pre_existing": ["a\\t.py::y"],
        "foreign": ["./a/t.py::z"],
    }
    assert ltt.reverify_live_failing(record) == ["a/t.py::x"]
    assert ltt.reverify_live_failing(None) == []
