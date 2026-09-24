"""
coordinator_core.warm.tests.test_indeterminate_message_makes_no_absence_claim
-- pins the retraction of the false "no trace means safe to re-run" claim
and the fire-and-forget/fail-closed message selection
(docs/decisions/DR-442-completion-evidence-is-the-engine-s-record-not-the-
side-effect.md).

Spec backlink: docs/plans/2026-09-23-completion-evidence-contract.md § C3

Test convention: pytest. Invoke via
``pytest coordinator_core/warm/tests/test_indeterminate_message_makes_no_absence_claim.py -v``
"""

from __future__ import annotations

import pytest

from coordinator_core.warm import client

_MSG = {"jsonrpc": "2.0", "id": 1, "method": "some.mutating.op", "params": {}}


def test_neither_constant_claims_absence_means_safe_to_rerun() -> None:
    for text in (
        client._MUTATION_INDETERMINATE_MESSAGE,
        client._OP_TIMEOUT_INDETERMINATE_MESSAGE,
        client._FIRE_AND_FORGET_INDETERMINATE_MESSAGE,
    ):
        assert "finding no trace means it is safe to re-run" not in text
        assert "Reconcile against real state" not in text


def test_envelope_selects_fire_and_forget_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.authz import completion_evidence

    monkeypatch.setattr(
        completion_evidence,
        "evidence_class",
        lambda op: completion_evidence.EvidenceClass.FIRE_AND_FORGET,
    )
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s")
    assert client._FIRE_AND_FORGET_INDETERMINATE_MESSAGE in envelope["error"]["message"]


def test_envelope_selects_fail_closed_text_when_undeclared(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.authz import completion_evidence

    monkeypatch.setattr(
        completion_evidence,
        "evidence_class",
        lambda op: completion_evidence.EvidenceClass.UNDECLARED,
    )
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s")
    assert client._MUTATION_INDETERMINATE_MESSAGE in envelope["error"]["message"]


def test_envelope_selects_fail_closed_text_when_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinator_core.authz import completion_evidence

    def _raise(op):
        raise RuntimeError("boom")

    monkeypatch.setattr(completion_evidence, "evidence_class", _raise)
    envelope = client._indeterminate_envelope(_MSG, "no response within 5s")
    assert client._MUTATION_INDETERMINATE_MESSAGE in envelope["error"]["message"]
