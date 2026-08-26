"""
Pins ``coordinator_core.subagent_sandbox.engine._canonical_agent_id``'s
named-teammate leg to DELEGATE to
``coordinator_core.session.identity.resolve_subagent_identity`` rather than
reimplementing the ``a<name>-<16hex>`` -> ``<name>@session-<short>`` grammar
a second time.

Spec backlink: state/dispatch-briefs/2026-08-25-a-named-dispatch-keeps-its-report/C2.md
Plan: docs/plans/2026-08-25-a-named-dispatch-keeps-its-report.md (AC1, AC11)

Negative-spec:
  - Does NOT re-derive the grammar independently -- every "resolves to X"
    assertion below is cross-checked against ``resolve_subagent_identity``
    itself (or ``build_canonical_agent_id``), never a hand-typed expected
    string alone, so a drift in the shared grammar fails this test instead
    of both copies silently agreeing on a stale answer.
  - Does NOT test the session_id-PRESENT and session_id-ABSENT legs as if
    they had one contract: the engine's F4 fallback is a DELIBERATE point of
    difference from ``resolve_subagent_identity`` (see engine.py docstring),
    not a bug to reconcile, and is pinned here as its own case rather than
    folded into the "agrees with identity.py" table.
"""

from __future__ import annotations

import pytest

from coordinator_core.session import identity as session_identity
from coordinator_core.subagent_sandbox import engine

BARE_HEX_AGENT_ID = "abc123def4567890"
NAMED_AGENT_ID = "aReviewBot-0123456789abcdef"
CANONICAL_TEAMMATE_AGENT_ID = "c7-agent-probe@session-2c79e462"

#: Both 2026-08-25 incident agents (docs/plans/2026-08-25-a-named-dispatch-
#: keeps-its-report.md) -- the raw subagent-side id the harness actually
#: presented, the session_id whose first 8 chars the canonical short-session
#: is truncated from, and the real EM-side canonical id recorded for each.
INCIDENT_AGENTS = [
    pytest.param(
        "arev-counter-tests-f4498a5559849145",
        "0bba1169-full-session-suffix-irrelevant",
        "rev-counter-tests@session-0bba1169",
        id="rev-counter-tests",
    ),
    pytest.param(
        "ayk-kill-ledger-value-4ac9e62cc2ac40b0",
        "ddabb4b7-full-session-suffix-irrelevant",
        "the VP-Product Reviewer-kill-ledger-value@session-ddabb4b7",
        id="yk-kill-ledger-value",
    ),
]


@pytest.mark.parametrize("raw_agent_id, session_id, expected_canonical", INCIDENT_AGENTS)
def test_incident_agent_ids_resolve_to_real_canonical_form(
    raw_agent_id: str, session_id: str, expected_canonical: str
) -> None:
    """AC1: both incident agents' raw ids now resolve to the EM-side
    canonical form the back-pointer directory is actually keyed by, instead
    of being returned unchanged as a bare format predicate."""
    assert engine._canonical_agent_id(raw_agent_id, session_id) == expected_canonical


@pytest.mark.parametrize("raw_agent_id, session_id, _expected", INCIDENT_AGENTS)
def test_engine_and_identity_agree_on_incident_agents(
    raw_agent_id: str, session_id: str, _expected: str
) -> None:
    assert engine._canonical_agent_id(
        raw_agent_id, session_id
    ) == session_identity.resolve_subagent_identity(raw_agent_id, session_id)


@pytest.mark.parametrize(
    "raw_agent_id, session_id",
    [
        (NAMED_AGENT_ID, "em-session-12345678"),
        ("aname-with-dashes-0123456789abcdef", "exactly8"),
    ],
)
def test_named_teammate_agrees_with_identity_on_shared_table(
    raw_agent_id: str, session_id: str
) -> None:
    """The engine and ``session/identity.py`` must agree over a shared table
    of named-teammate ids, including a boundary session_id of exactly 8
    chars (the strict >=8 boundary identity.py's own docstring names)."""
    assert engine._canonical_agent_id(
        raw_agent_id, session_id
    ) == session_identity.resolve_subagent_identity(raw_agent_id, session_id)


def test_named_teammate_trailing_newline_id_agrees_with_identity() -> None:
    """Shared table entry covering the trailing-newline-before-`$` gap both
    resolvers close via ``fullmatch`` (not ``match``)."""
    raw = NAMED_AGENT_ID + "\n"
    session_id = "em-session-1"
    assert engine._canonical_agent_id(raw, session_id) == "" == (
        session_identity.resolve_subagent_identity(raw, session_id)
    )


def test_bare_hex_unchanged() -> None:
    assert engine._canonical_agent_id(BARE_HEX_AGENT_ID, None) == BARE_HEX_AGENT_ID


def test_unrecognized_shape_unchanged_to_empty() -> None:
    assert engine._canonical_agent_id("not-a-valid-id", None) == ""


def test_em_side_canonical_form_passed_through_unchanged() -> None:
    """Form (c) -- already the EM-side canonical shape -- is not something
    ``resolve_subagent_identity`` itself accepts (its leg (b) only matches
    the raw ``a<name>-<16hex>`` shape), so this leg stays a local format
    predicate rather than being delegated, and must remain unchanged."""
    assert (
        engine._canonical_agent_id(CANONICAL_TEAMMATE_AGENT_ID, "em-session-1")
        == CANONICAL_TEAMMATE_AGENT_ID
    )
    assert (
        engine._canonical_agent_id(CANONICAL_TEAMMATE_AGENT_ID, None)
        == CANONICAL_TEAMMATE_AGENT_ID
    )


@pytest.mark.parametrize(
    "session_id",
    [None, "", "short7"],
)
def test_named_teammate_session_id_absent_or_short_retains_f4_fallback(
    session_id: object,
) -> None:
    """AC11: a named ``agent_id`` with ``session_id`` absent or shorter than
    8 chars still resolves to a non-empty id via the retained F4 fallback --
    the raw agent_id itself -- rather than the ``''``
    ``resolve_subagent_identity`` alone would produce for that same input."""
    assert session_identity.resolve_subagent_identity(NAMED_AGENT_ID, session_id or "") == ""
    result = engine._canonical_agent_id(NAMED_AGENT_ID, session_id)  # type: ignore[arg-type]
    assert result == NAMED_AGENT_ID
    assert result != ""
