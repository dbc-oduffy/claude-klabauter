"""gated_rows: every gated exclusion becomes a GatedRow, in order, never a refusal."""

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import GatedRow
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import gated_rows

RAW = {
    "P1": {
        "id": "P1",
        "external_gate": [
            {"owner_repo": "C:/example-cockpit-repo", "requires": "commit-in-owner-repo"}  # abs-path-ok: fixture needs a drive-letter path verbatim
        ],
    },
    "P2": {
        "id": "P2",
        "external_gate": [{"owner_repo": "/home/u/cockpit", "requires": "owner-commit"}],
    },
}


def test_external_and_transitive_rows_in_order():
    exclusions = [
        {"id": "P1", "reason": "external_gate", "detail": "d"},
        {"id": "C9", "reason": "transitive_gate_closure", "detail": "via P1"},
    ]
    assert gated_rows(exclusions, RAW) == [
        GatedRow(
            "P1",
            "external_gate",
            "owner_repo=C:/example-cockpit-repo requires=commit-in-owner-repo",  # abs-path-ok: fixture
        ),
        GatedRow("C9", "transitive_gate_closure", "via P1"),
    ]


def test_two_external_rows_keep_exclusion_order():
    exclusions = [
        {"id": "P2", "reason": "external_gate", "detail": "d"},
        {"id": "P1", "reason": "external_gate", "detail": "d"},
    ]
    got = gated_rows(exclusions, RAW)
    assert [g.id for g in got] == ["P2", "P1"]
    assert got[0].gate == "owner_repo=/home/u/cockpit requires=owner-commit"


def test_gated_row_without_raw_entry_still_listed():
    got = gated_rows([{"id": "Z", "reason": "external_gate", "detail": "d"}], {})
    assert got == [GatedRow("Z", "external_gate", "external_gate")]


@pytest.mark.parametrize(
    "reason", ["disposition", "deferred", "em-performed", "operator"]
)
def test_other_reasons_are_skipped(reason):
    assert gated_rows([{"id": "A", "reason": reason, "detail": "d"}], {}) == []


def test_empty():
    assert gated_rows([], {}) == []
