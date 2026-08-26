"""
coordinator_core.reconcile.tests.test_surfaced_false_positive_rate -- C5 oracle
fixture (docs/plans/2026-08-25-reconcile-open-comes-back-under-the-bar.md,
Claude-klabauter).

Discharges AC7: "The audit's 25-row oracle is a fixture in this repo, with each
row's expected verdict, so the false-positive rate is measurable here rather
than by re-reading a sibling repo's audit." This module reads the ported
fixture (`fixtures/stale_record_triage_oracle.yaml`, itself a port of
DoE-claude `state/audits/2026-07-20-stale-record-triage.md` § Group C) and
computes the false-positive rate straight from that static table.

Also asserts the AC11 precondition every downstream chunk (C6, C7) depends on:
the fixture carries BOTH polarities. A detector fix that suppresses every
surfaced row would pass a naive "false positives -> 0" check trivially if the
oracle had no true-positive rows left to check against -- this test fails in
that scenario by asserting `true_positive_ids` is non-empty and that the
`sat`-family false positives (AC8's target) do not swallow any real
`is_false_positive: false` row.

Negative-spec: does NOT import or call `gate_eval.py` or `commit_reality.py`
-- this module only proves the oracle DATA is internally consistent and
loadable; wiring the actual detectors against it is C6/C7's job (both
`depends_on: C5, gate_kind: output-consumption-runtime`), not this chunk's.

C12 ADDITION (docs/plans/2026-08-25-reconcile-open-comes-back-under-the-bar.md
§ C12, staff-eng Finding 7): AC10/AC11 were restated 2026-08-26 to the
`gate_eval` sub-table alone (14 rows, 7 originally false) once C10 deleted
`commit_reality`'s verdict -- C6 landed the asymmetry-detector fix for 6 of
those 7 false positives but never took the measurement against either
denominator. The tests below restate that arithmetic against the 14-row
denominator; the negative-spec above is UNCHANGED by this addition -- the
fix itself is proven live, against the real symmetric sat graph, by
`test_gate_eval.py::TestSatFamilyOracleAsymmetryFalsePositivesGoToZero`
(which this module still does not import), not re-derived here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "stale_record_triage_oracle.yaml"

#: Verdicts the ported audit table actually uses (see the fixture's own row
#: data) -- a row carrying anything else is a transcription defect, not a
#: legitimate new verdict this test should silently accept.
_KNOWN_VERDICTS = {
    "FLIP-TO-IMPLEMENTED",
    "EXECUTE",
    "NEEDS-PM",
    "DETECTOR-BUG",
    "NO-ACTION",
}


def _load_oracle() -> Dict[str, Any]:
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_fixture_loads_and_declares_its_source() -> None:
    oracle = _load_oracle()
    assert oracle["source"] == "DoE-claude state/audits/2026-07-20-stale-record-triage.md"
    assert oracle["source_section"] == "Group C -- auto-reconcile surfaced records"
    assert isinstance(oracle["rows"], list)
    assert len(oracle["rows"]) == oracle["ported_total_rows"]


def test_every_row_has_a_unique_id_and_a_known_verdict() -> None:
    oracle = _load_oracle()
    rows: List[Dict[str, Any]] = oracle["rows"]
    seen_ids = set()
    for row in rows:
        assert row["id"], f"row missing id: {row!r}"
        assert row["id"] not in seen_ids, f"duplicate id: {row['id']!r}"
        seen_ids.add(row["id"])
        assert row["audit_verdict"] in _KNOWN_VERDICTS, (
            f"{row['id']!r} carries an unrecognised verdict {row['audit_verdict']!r}"
        )
        assert isinstance(row["is_false_positive"], bool)
        assert row["evidence"].strip(), f"{row['id']!r} has no evidence text"


def test_false_positive_rate_is_measurable_from_the_fixture_alone() -> None:
    """AC7's own text: the rate is measurable here, not by re-reading DoE's
    audit. Computed straight off `is_false_positive`, cross-checked against
    the fixture's own declared totals so a hand-edit that drifts the two
    apart fails loudly here rather than silently downstream in C6/C7."""
    oracle = _load_oracle()
    rows: List[Dict[str, Any]] = oracle["rows"]

    false_positive_ids = [r["id"] for r in rows if r["is_false_positive"]]
    true_positive_ids = [r["id"] for r in rows if not r["is_false_positive"]]

    assert len(rows) == oracle["ported_total_rows"]
    assert len(false_positive_ids) == oracle["ported_false_positives"]
    assert len(false_positive_ids) + len(true_positive_ids) == len(rows)

    # AC11 precondition: both polarities must be non-empty, or a detector
    # fix that surfaces nothing has nothing here to fail it.
    assert true_positive_ids, "oracle has no true-positive rows -- AC11 unfalsifiable"
    assert false_positive_ids, "oracle has no false-positive rows -- AC10 unmeasurable"


def test_sat_family_asymmetry_false_positives_are_all_present() -> None:
    """AC8's target: 6 `sat`-family false positives, all attributed to the
    same named detector bug (`asymmetry_detector_bare_stub_id`), none of them
    silently merged or dropped during the port."""
    oracle = _load_oracle()
    rows: List[Dict[str, Any]] = oracle["rows"]

    sat_rows = [r for r in rows if "roadmap-sat-" in r["id"]]
    assert len(sat_rows) == 6
    assert all(r["is_false_positive"] for r in sat_rows)
    assert all(r.get("detector_bug") == "asymmetry_detector_bare_stub_id" for r in sat_rows)
    assert all(r["audit_verdict"] == "DETECTOR-BUG" for r in sat_rows)


def test_commit_reality_ambiguous_attribution_rows_are_tagged() -> None:
    """AC9's target family: `commit_reality` rows the audit calls out as
    spurious attribution on `coordinator_core/` scope breadth alone. These
    are tagged `detector_bug: ambiguous_attribution_scope_breadth` but
    remain `is_false_positive: false` -- the audit's own verdict is that
    real residual work exists even though the cited commit is the wrong one,
    so a fix here must not make these rows go quiet (AC11)."""
    oracle = _load_oracle()
    rows: List[Dict[str, Any]] = oracle["rows"]

    ambiguous_rows = [
        r for r in rows if r.get("detector_bug") == "ambiguous_attribution_scope_breadth"
    ]
    assert len(ambiguous_rows) == 3
    assert all(r["detector"] == "commit_reality" for r in ambiguous_rows)
    assert all(not r["is_false_positive"] for r in ambiguous_rows)


#: C6's own live pinning tests (test_gate_eval.py::
#: TestSatFamilyOracleAsymmetryFalsePositivesGoToZero) prove these 6
#: detector_bug-tagged rows no longer surface an asymmetry finding against
#: the real, symmetric sat graph -- referenced by name, not imported, per
#: this module's negative-spec (see module docstring, C12 ADDITION).
_FIXED_DETECTOR_BUG = "asymmetry_detector_bare_stub_id"


def _gate_eval_rows(oracle: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [r for r in oracle["rows"] if r["detector"] == "gate_eval"]


def test_gate_eval_restated_denominator_is_14_rows() -> None:
    """AC10/AC11 restated denominator (staff-eng Finding 7): C10 deletes the
    `commit_reality` verdict, so the false-positive rate is measured over the
    `gate_eval` sub-table alone (14 rows), not the 24-row combined oracle."""
    oracle = _load_oracle()
    assert len(_gate_eval_rows(oracle)) == 14


def test_ac10_residual_false_positive_count_is_one_of_fourteen() -> None:
    """AC10 (restated 2026-08-26) -- false positives over the 14 surviving
    `gate_eval` rows, expected residual <=1 (from 7). Of the fixture's 7
    originally-false `gate_eval` rows, 6 carry `detector_bug:
    asymmetry_detector_bare_stub_id` -- C6's already-landed fix, proven live
    against the real sat graph by
    `test_gate_eval.py::TestSatFamilyOracleAsymmetryFalsePositivesGoToZero`.
    The residual is the single untagged row `2026-07-04_125734_f3a5324e`
    (`FLIP-TO-IMPLEMENTED`, gate already satisfied) -- no chunk targets it, so
    it remains a false positive. Residual == 1, not 0, so the AC11 interlock
    below is not vacuous."""
    oracle = _load_oracle()
    false_positive_rows = [r for r in _gate_eval_rows(oracle) if r["is_false_positive"]]
    assert len(false_positive_rows) == 7, "fixture's own gate_eval false-positive count drifted"

    fixed_rows = [r for r in false_positive_rows if r.get("detector_bug") == _FIXED_DETECTOR_BUG]
    assert len(fixed_rows) == 6

    residual_rows = [
        r for r in false_positive_rows if r.get("detector_bug") != _FIXED_DETECTOR_BUG
    ]
    assert [r["id"] for r in residual_rows] == ["2026-07-04_125734_f3a5324e"]
    assert len(residual_rows) == 1, "AC10 residual false-positive count must be <=1 of 14"


def test_ac11_all_seven_true_positives_still_surface() -> None:
    """AC11 (re-scoped 2026-08-26 to gate_eval's 7 true-positive rows) --
    every `gate_eval` row the fixture marks a genuine finding
    (`is_false_positive: false`) is still present in the restated
    denominator. This is the interlock the chunk brief calls out in the same
    breath as AC10: a detector fix that suppresses everything would pass
    AC10 trivially by having nothing left to surface -- checked here, in the
    same pass, never after."""
    oracle = _load_oracle()
    true_positive_rows = [r for r in _gate_eval_rows(oracle) if not r["is_false_positive"]]
    assert len(true_positive_rows) == 7
    assert {r["id"] for r in true_positive_rows} == {
        "2026-06-30_021546_12e715f3",
        "2026-07-06_210200_strang-10-inject-anchor-archive-carveout",
        "2026-07-08_151948_afec14f4",
        "2026-07-10-engine-migration-bin-lib-bulk-port",
        "2026-07-10-engine-migration-high-traffic-strangle",
        "2026-07-10-engine-migration-hooks-and-sessionstart-residual",
        "2026-07-10_141606_roadmap-qsub-03",
    }


def test_plan_headline_totals_are_recorded_but_not_asserted_as_fact() -> None:
    """The plan's own AC7/AC10 text calls this "the audit's 25-row oracle"
    with "15 of 25" false positives; the audit's Group C tables, expanded to
    one row per underlying record, enumerate 24 records (11 explicitly
    false-positive). This test pins that KNOWN DISCREPANCY (see the
    fixture's own header comment) so a future edit that quietly reconciles
    the two without re-reading the source tables is caught -- it does not
    assert the plan headline figures as fact, only that they are recorded
    verbatim alongside the ported figures for whoever resolves the gap."""
    oracle = _load_oracle()
    assert oracle["plan_headline_total_rows"] == 25
    assert oracle["plan_headline_false_positives"] == 15
    assert oracle["ported_total_rows"] == 24
    assert oracle["ported_false_positives"] == 11
