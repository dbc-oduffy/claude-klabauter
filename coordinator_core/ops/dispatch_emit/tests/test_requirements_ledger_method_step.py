"""judge() refuses a met method-step row whose plan carries no matching mechanism trace."""

from __future__ import annotations

import pytest

from coordinator_core.ops import method_step
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

SLUG = "likeness-method"
ITEMS = (
    ("R1", "(a) capture the scan"),
    ("R2", "(b) map into MetaHuman expected-value space"),
)


def _ledger():
    items = tuple(
        rl.Item(rid, text, "relayed", "", method_step.mark_anchor("relayed", SLUG, text[1]), "")
        for rid, text in ITEMS
    )
    return rl.Ledger(items, False, "state/sizings/x.yaml", ())


def _verdict(**extra):
    return {"items": [{"id": rid, "status": "met", **extra.get(rid, {})} for rid, _ in ITEMS]}


def _fence(row, mechanism, at, deviation=""):
    return (
        f"```yaml method-trace\nrow: {row}\nmechanism: {mechanism}\n"
        f"at: {{path: impl.py, contains: {at}}}\n{deviation}```\n"
    )


@pytest.fixture
def root(tmp_path):
    (tmp_path / "impl.py").write_text("def map_expected_value_space(): pass\n", encoding="utf-8")
    return tmp_path


GOOD_R1 = _fence("R1", "capture the scan from the device", "map_expected_value_space")
GOOD_R2 = _fence("R2", "map values into the MetaHuman expected-value space table", "map_expected_value_space")


def _reasons(out, rid):
    return [f["reason"] for f in out["failures"] if f["id"] == rid]


def test_traced_steps_pass(root):
    out = rl.judge(_ledger(), _verdict(), repo_root=root, plan_text=GOOD_R1 + GOOD_R2)
    assert out["pass"], out


def test_step_without_trace_is_refused(root):
    out = rl.judge(_ledger(), _verdict(), repo_root=root, plan_text=GOOD_R1)
    assert not out["pass"]
    assert any("no mechanism trace" in r for r in _reasons(out, "R2"))
    assert not _reasons(out, "R1")


def test_name_only_mechanism_is_refused(root):
    plan = GOOD_R1 + _fence("R2", "the likeness method step b", "map_expected_value_space")
    out = rl.judge(_ledger(), _verdict(), repo_root=root, plan_text=plan)
    assert any("names the step" in r for r in _reasons(out, "R2"))


def test_different_mechanism_without_deviation_is_refused_and_with_ratified_deviation_passes(root):
    swapped = _fence("R2", "conform scan values onto the mesh", "map_expected_value_space")
    out = rl.judge(_ledger(), _verdict(), repo_root=root, plan_text=GOOD_R1 + swapped)
    assert any("under half" in r for r in _reasons(out, "R2"))

    dev = "deviation: {delivers_by: mesh conform, reason: cheaper, ratified_by: PM ruling}\n"
    ok = _fence("R2", "conform scan values onto the mesh", "map_expected_value_space", dev)
    assert rl.judge(_ledger(), _verdict(), repo_root=root, plan_text=GOOD_R1 + ok)["pass"]


def test_mechanism_absent_from_tree_fails_even_when_item_is_met(root):
    absent = _fence("R2", "map values into the MetaHuman expected-value space table", "no_such_symbol")
    out = rl.judge(_ledger(), _verdict(), repo_root=root, plan_text=GOOD_R1 + absent)
    assert any("absent" in r for r in _reasons(out, "R2"))


def test_unmarked_row_carries_no_trace_burden(root):
    ledger = rl.Ledger((rl.Item("R1", "(a) plain", "relayed", "", "relayed", ""),), False, "x", ())
    out = rl.judge(ledger, {"items": [{"id": "R1", "status": "met"}]}, repo_root=root, plan_text="")
    assert out["pass"], out


def test_method_lead_in_marks_every_lettered_part():
    entries = rl._lettered_entries("Use a three-step likeness method: (a) capture the scan (b) map values (c) bake")
    assert [e[0][:3] for e in entries] == ["(a)", "(b)", "(c)"]
    for _, anchor in entries:
        assert method_step.split_anchor(anchor)[0] == "relayed"
        assert anchor != "relayed"


def test_plain_lettered_list_mints_unmarked_rows():
    entries = rl._lettered_entries("Please fix: (a) the logo (b) the footer")
    assert entries and all(anchor == "relayed" for _, anchor in entries)


def test_marked_anchor_still_reads_as_relayed_origin():
    anchor = method_step.mark_anchor("relayed", SLUG, "a")
    assert rl._origin(anchor) == ("relayed", "")


def test_no_repo_root_refuses_a_met_step(root):
    out = rl.judge(_ledger(), _verdict(), plan_text=GOOD_R1 + GOOD_R2)
    assert any("no repo root" in r for r in _reasons(out, "R1"))
