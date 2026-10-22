"""Tranche selection groups plans by declared file and `writes_under` prefix, never by a derived parent directory."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import inventory_mint as im


def _row(item: str, writes: list, under: list | None = None) -> dict:
    row = {"id": f"{item}.r", "writes": writes}
    if under:
        row["writes_under"] = under
    return row


def _select(*plans: tuple) -> tuple:
    items = [p[0] for p in plans]
    rows = [_row(*p) for p in plans]
    return im.select_tranche(rows, items, 100)


def test_distinct_evidence_sidecars_in_one_dir_are_not_deferred_as_same_path():
    selected, report = _select(
        ("I1", ["docs/plans/a.evidence.yaml"]),
        ("I2", ["docs/plans/b.evidence.yaml"]),
    )
    assert report["plans"] == ["I1", "I2"]
    assert not report["deferred"]
    assert {r["id"] for r in selected} == {"I1.r", "I2.r"}


def test_same_file_still_groups_and_names_the_path():
    groups, shared = im._shared_path_groups(
        [_row("I1", ["docs/plans/a.evidence.yaml"]), _row("I2", ["docs\\plans\\a.evidence.yaml"])],
        ["I1", "I2"],
        ["I1", "I2"],
    )
    assert groups == [["I1", "I2"]]
    assert shared["I1"] == "docs/plans/a.evidence.yaml"


def test_writes_under_prefix_still_groups_with_a_file_beneath_it():
    groups, shared = im._shared_path_groups(
        [_row("I1", [], ["docs/plans/"]), _row("I2", ["docs/plans/b.evidence.yaml"]), _row("I3", ["src/z.py"])],
        ["I1", "I2", "I3"],
        ["I1", "I2", "I3"],
    )
    assert groups == [["I1", "I2"], ["I3"]]
    assert shared["I1"] == "docs/plans"


def test_twenty_six_distinct_sidecars_land_in_one_tranche():
    plans = [(f"I{n}", [f"docs/plans/p{n}.evidence.yaml"]) for n in range(26)]
    selected, report = _select(*plans)
    assert len(report["plans"]) == 26 and not report["deferred"]
