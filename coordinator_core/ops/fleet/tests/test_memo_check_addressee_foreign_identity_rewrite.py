"""Regression for the C3-style foreign-repo-name rewrite in
`compute_check_addressee_candidate`'s UNRESOLVED-central-id note (post-close
review sweep, 2026-08-30).

Spec backlink: docs/plans/2026-08-30-the-engine-stops-naming-its-own-repo.md § C3;
correction to `state/audits/2026-08-30-foreign-repo-identity-disposition-probe.md`
row 26, which had classified this site NOT-REACHABLE. That verdict was wrong on
reachability: `compute_check_addressee_candidate` is imported by
`coordinator_core/pickup_assemble/__init__.py` and driven by
`compute_addressee_gate`, the `/pickup` M-addr guard, which runs on every
`/pickup` once a `to_value` is present — not only via an operator-invoked fleet
memo command. The rendered note is INCIDENTAL (the diagnosis is fully actionable
without naming which repo the id resolves to), so the repo name is swapped for a
generic phrase, following the shape `forwarder_drift.py`'s skip-line rewrite used.

Negative-spec: the rendered note must never contain the literal `DoE-claude`,
in any casing this function could plausibly emit, while still surfacing the
receiver id and the `identity.centralReceiverIds` attribute path the reader
needs to act on the diagnosis.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet import memo_check_addressee as mca


def test_unresolved_central_id_note_never_names_doe_claude(monkeypatch):
    monkeypatch.setattr(mca, "read_redirect_aliases", lambda: set())
    monkeypatch.setattr(mca, "read_central_receiver_ids", lambda: {"central-hub"})
    monkeypatch.setattr(
        mca, "resolve_receiver_inbox", lambda to: (None, None, {})
    )

    candidate = mca.compute_check_addressee_candidate(
        Path("/repo/self"), "central-hub"
    )

    note = candidate["note"]
    assert note is not None
    assert "DoE-claude" not in note
    assert "doe-claude" not in note.lower()


def test_unresolved_central_id_note_stays_actionable(monkeypatch):
    monkeypatch.setattr(mca, "read_redirect_aliases", lambda: set())
    monkeypatch.setattr(mca, "read_central_receiver_ids", lambda: {"central-hub"})
    monkeypatch.setattr(
        mca, "resolve_receiver_inbox", lambda to: (None, None, {})
    )

    candidate = mca.compute_check_addressee_candidate(
        Path("/repo/self"), "central-hub"
    )

    note = candidate["note"]
    assert note is not None
    assert "central-hub" in note
    assert "identity.centralReceiverIds" in note
    assert "registered in the machine-local registry" in note
