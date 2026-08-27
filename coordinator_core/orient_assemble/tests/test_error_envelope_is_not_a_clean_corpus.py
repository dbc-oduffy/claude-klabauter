"""
coordinator_core.orient_assemble.tests.test_error_envelope_is_not_a_clean_corpus
— pins that a FAILED `handoff.reconcile_open` probe never renders as
"nothing to reconcile."

A JSON-RPC error envelope carries no `result`, so `result.get("surfaced")`
is `[]` — byte-identical, downstream of `_read_auto_reconcile`, to a healthy
probe that found a clean corpus. Before `_auto_reconcile_probe_failed`
existed, both produced an empty `ReaderResult()` and the Morning Briefing
omitted `### Auto-Reconcile` entirely. Measured 2026-08-27: the op had
produced zero op-latency rows in ~10h against 3,301 rows from its peers,
and no surface anywhere said so.

Negative-spec:
    - Does NOT assert the error CODE or message text verbatim beyond their
      presence in `evidence` — any JSON-RPC error class must surface, not
      one enumerated list of them.
    - Does NOT assert a directive is emitted — an unreachable probe is a
      fact for the reader, never an action this module applies.
"""

from __future__ import annotations

from coordinator_core.orient_assemble import readers_branch_reconcile as rbr


def _patch_error(monkeypatch, envelope: dict) -> None:
    import coordinator_core.ops.check_auto_reconcile as check_auto_reconcile

    monkeypatch.setattr(check_auto_reconcile, "get_response", lambda: envelope)


def test_error_envelope_surfaces_a_judgment_point(monkeypatch):
    """The regression this file exists for: an errored probe must be
    distinguishable from a clean corpus."""
    _patch_error(
        monkeypatch,
        {
            "jsonrpc": "2.0",
            "error": {"code": -32005, "message": "dispatch refused: no build stamp"},
        },
    )

    result = rbr._read_auto_reconcile()

    assert result.judgment_points, (
        "an errored auto-reconcile probe produced an empty ReaderResult — "
        "indistinguishable from a clean corpus"
    )
    assert not result.directives
    jp = result.judgment_points[0]
    assert jp["id"] == "j-auto-reconcile-probe-failed"
    assert "-32005" in jp["evidence"]
    assert "dispatch refused" in jp["evidence"]


def test_clean_corpus_still_stays_silent(monkeypatch):
    """The other half of the distinction: a healthy probe with nothing
    surfaced must still contribute nothing, or every briefing grows a
    permanent empty section."""
    _patch_error(monkeypatch, {"jsonrpc": "2.0", "result": {"surfaced": []}})

    result = rbr._read_auto_reconcile()

    assert not result.judgment_points
    assert not result.directives


def test_none_response_still_stays_silent(monkeypatch):
    """`get_response()` returning None is `check_auto_reconcile`'s own
    documented silent-skip contract (not-a-git-repo) — this branch must not
    hijack it."""
    _patch_error(monkeypatch, None)

    result = rbr._read_auto_reconcile()

    assert not result.judgment_points
    assert not result.directives
