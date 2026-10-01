"""An install-clone-rooted session emits one install-clone ledger row, not could-not-establish noise."""

from __future__ import annotations

from coordinator_core.ops.ceremony.branch_resolution import INSTALL_CLONE_FINDING
from coordinator_core.quick_wrap_assemble import _LEDGER_COULD_NOT_ESTABLISH, _close_ledger


def _degraded(evidence: str) -> dict:
    return {"degraded": True, "evidence": evidence}


def _gate() -> dict:
    return {
        "pickup_kind": _degraded("a"),
        "governing_plan": _degraded("b"),
        "diff": _degraded("c"),
    }


def test_install_clone_collapses_to_one_finding():
    entries = _close_ledger(_gate(), install_clone=True)
    assert len(entries) == 1
    assert entries[0]["status"] == _LEDGER_COULD_NOT_ESTABLISH
    assert entries[0]["evidence"] == INSTALL_CLONE_FINDING


def test_ordinary_root_keeps_per_leg_rows():
    assert len(_close_ledger(_gate())) == 3
