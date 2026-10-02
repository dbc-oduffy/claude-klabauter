"""check_external_gate_exclusions: refuse, never silently drop, gated rows."""

import pytest

from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    ExternalGateRowsRefused,
    check_external_gate_exclusions,
)

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


def test_external_and_transitive_rows_named_in_order():
    exclusions = [
        {"id": "P1", "reason": "external_gate", "detail": "d"},
        {"id": "P2", "reason": "external_gate", "detail": "d"},
        {"id": "C9", "reason": "transitive_gate_closure", "detail": "via P1"},
    ]
    with pytest.raises(ExternalGateRowsRefused) as info:
        check_external_gate_exclusions(exclusions, RAW)
    exc = info.value
    assert exc.rows == ["P1", "P2", "C9"]
    msg = str(exc)
    assert msg.startswith("external_gate rows cannot run in this workflow:")
    assert "P1: owner_repo=C:/example-cockpit-repo requires=commit-in-owner-repo" in msg
    assert "P2: owner_repo=/home/u/cockpit requires=owner-commit" in msg
    assert "C9: transitively gated (via P1)" in msg
    assert isinstance(exc, CrossRepoWriteError)


def test_gated_row_without_raw_entry_still_named():
    with pytest.raises(ExternalGateRowsRefused) as info:
        check_external_gate_exclusions(
            [{"id": "Z", "reason": "external_gate", "detail": "d"}], {}
        )
    assert info.value.rows == ["Z"]


@pytest.mark.parametrize(
    "reason", ["disposition", "deferred", "em-performed", "operator"]
)
def test_other_reasons_do_not_raise(reason):
    check_external_gate_exclusions([{"id": "A", "reason": reason, "detail": "d"}], {})


def test_empty_does_not_raise():
    check_external_gate_exclusions([], {})
