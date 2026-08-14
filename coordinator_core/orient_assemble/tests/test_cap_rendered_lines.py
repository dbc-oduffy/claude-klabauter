"""
Tests for coordinator_core.orient_assemble.readers_handoff_triage's
`_cap_rendered_lines` / `_cap_awaiting_gate_listing` — the post-hoc line
caps that bound `_read_ready`/`_read_awaiting_gate`'s otherwise-unbounded
captured-stdout text.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420,
chunk C2b; state/bug-backlog/2026-08-13-session-brief-byte-budget-assertion
-is-r-8733361330d6.yaml (the incident these caps were added to fix).

Review: coordinatorcode-reviewer-87b5ce47 — Finding [P1] (this cap shipped
with zero direct unit coverage; the only related test,
`test_brief_session_stays_under_byte_budget`, is a live-disk integration
byte-budget assertion that would still pass with the cap off by several
lines) — and Finding [P2]/[P3] (the awaiting-gate two-listing concatenation
could silently swallow the whole stale-escalated tier, and the withheld
count could be off by one against a separator line).

Negative-spec:
    - Does NOT exercise `_cmd_ready`/`_cmd_awaiting_gate` or real disk
      content — these are unit tests of the pure text-capping functions,
      matching `test_readers_handoff_triage_claim.py`'s own "unit test of
      the post-hoc line filter, not the ported CLI's query logic" scoping.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.orient_assemble import readers_handoff_triage as rht


def _lines(n: int, *, prefix: str = "line") -> str:
    return "\n".join(f"{prefix}-{i}" for i in range(n))


# ---------------------------------------------------------------------------
# _cap_rendered_lines
# ---------------------------------------------------------------------------


def test_cap_rendered_lines_unchanged_at_exactly_cap_lines():
    """Text at exactly `cap` lines is returned byte-for-byte unchanged — no
    "+K more" line, no trailing blank line appended."""
    text = _lines(5)
    result = rht._cap_rendered_lines(text, 5, subcommand="ready")
    assert result == text


def test_cap_rendered_lines_unchanged_under_cap():
    text = _lines(3)
    result = rht._cap_rendered_lines(text, 5, subcommand="ready")
    assert result == text


def test_cap_rendered_lines_appends_exactly_one_more_line_when_over():
    """One line over cap: exactly one "+1 more" notice, naming the given
    subcommand, and the kept lines are an unmodified, unreordered prefix."""
    text = _lines(6)
    result = rht._cap_rendered_lines(text, 5, subcommand="ready")
    result_lines = result.split("\n")

    assert result_lines[:5] == [f"line-{i}" for i in range(5)]
    # Finding [P3] fix: a blank line separates the kept body from the
    # notice so a markdown renderer can't parse it as a list continuation.
    assert result_lines[5] == ""
    assert result_lines[6] == (
        "+1 more — run `workday-start-handoff-triage ready` to see them all"
    )
    assert len(result_lines) == 7
    # Exactly one "+K more" notice — never more than one.
    assert sum(1 for ln in result_lines if "more —" in ln) == 1


def test_cap_rendered_lines_withheld_count_matches_actual_withheld_lines():
    text = _lines(20)
    result = rht._cap_rendered_lines(text, 15, subcommand="awaiting-gate")
    result_lines = result.split("\n")
    assert result_lines[-1] == (
        "+5 more — run `workday-start-handoff-triage awaiting-gate` "
        "to see them all"
    )


def test_cap_rendered_lines_empty_input():
    """A single empty string (split("\n") of "") is one line — under any
    cap >= 1, returned unchanged."""
    result = rht._cap_rendered_lines("", 5, subcommand="ready")
    assert result == ""


def test_cap_rendered_lines_single_line_input():
    text = "- [only one](state/handoffs/x.md) — open"
    result = rht._cap_rendered_lines(text, 5, subcommand="ready")
    assert result == text


# ---------------------------------------------------------------------------
# _cap_awaiting_gate_listing — Finding [P2]/[P3]
# ---------------------------------------------------------------------------


def test_awaiting_gate_stale_tier_survives_when_full_listing_alone_hits_cap():
    """The defect Finding [P2] names: the full listing alone reaches `cap`
    — capping the naive concatenation would drop the separator and the
    ENTIRE stale section with no signal. Capping each section independently
    keeps the stale tier visible."""
    full = _lines(20, prefix="full")
    stale = _lines(3, prefix="stale")
    text = f"{full}\n{rht._AWAITING_GATE_SEPARATOR}\n{stale}"

    result = rht._cap_awaiting_gate_listing(text, 15, subcommand="awaiting-gate")

    assert rht._AWAITING_GATE_SEPARATOR in result
    assert "stale-0" in result
    assert "stale-1" in result
    assert "stale-2" in result
    # The full section was capped and carries its own withheld notice.
    assert "+5 more" in result


def test_awaiting_gate_both_sections_capped_independently():
    """Both sections over cap: each gets its OWN "+K more" notice, scoped
    to its own withheld count — not a single notice over the combined
    total."""
    full = _lines(18, prefix="full")
    stale = _lines(17, prefix="stale")
    text = f"{full}\n{rht._AWAITING_GATE_SEPARATOR}\n{stale}"

    result = rht._cap_awaiting_gate_listing(text, 15, subcommand="awaiting-gate")

    assert result.count("more —") == 2
    assert "+3 more" in result  # full: 18 - 15
    assert "+2 more" in result  # stale: 17 - 15


def test_awaiting_gate_withheld_count_excludes_separator_line():
    """Finding [P3]: the separator line must not be counted toward either
    section's withheld tally — splitting on it before capping means neither
    `_cap_rendered_lines` call ever sees it."""
    full = _lines(16, prefix="full")
    stale = _lines(2, prefix="stale")
    text = f"{full}\n{rht._AWAITING_GATE_SEPARATOR}\n{stale}"

    result = rht._cap_awaiting_gate_listing(text, 15, subcommand="awaiting-gate")

    # Exactly 1 withheld from the full section (16 - 15), not 2 (which
    # would mean the separator line got folded into the full section's
    # line count).
    assert "+1 more" in result
    assert "+2 more" not in result


def test_awaiting_gate_no_separator_falls_through_to_plain_cap():
    """No stale subset present (the separator never printed) — behaves
    identically to `_cap_rendered_lines` on the single listing."""
    text = _lines(20, prefix="full")
    result = rht._cap_awaiting_gate_listing(text, 15, subcommand="awaiting-gate")
    expected = rht._cap_rendered_lines(text, 15, subcommand="awaiting-gate")
    assert result == expected


def test_awaiting_gate_both_sections_under_cap_returns_reassembled_unchanged():
    full = _lines(3, prefix="full")
    stale = _lines(2, prefix="stale")
    text = f"{full}\n{rht._AWAITING_GATE_SEPARATOR}\n{stale}"

    result = rht._cap_awaiting_gate_listing(text, 15, subcommand="awaiting-gate")

    assert result == text


def test_awaiting_gate_empty_full_listing_omits_leading_blank_line():
    """Reachable per `_cmd_awaiting_gate`: the full `awaiting_gate` listing
    prints unconditionally (including when empty), while the separator
    prints only when the stale subset is non-empty — so an empty full
    listing paired with a non-empty stale subset is a real input shape, not
    hypothetical (Review: code-reviewer — Finding [P3]). The reassembled
    output must start directly with the separator, not a blank line."""
    stale = _lines(2, prefix="stale")
    text = f"\n{rht._AWAITING_GATE_SEPARATOR}\n{stale}"

    result = rht._cap_awaiting_gate_listing(text, 15, subcommand="awaiting-gate")

    assert result == f"{rht._AWAITING_GATE_SEPARATOR}\n{stale}"
    assert not result.startswith("\n")


def test_separator_literal_still_matches_the_ported_cli_source():
    """Pins the cross-file coupling `_cap_awaiting_gate_listing` rests on.

    Review: code-reviewer — Finding [P4]. `_AWAITING_GATE_SEPARATOR` duplicates
    a literal `coordinator/bin/workday-start-handoff-triage.py` prints; there is
    no shared symbol between the two files. If the CLI's string drifts,
    `text.find(...)` returns -1 and the helper falls through to the
    single-listing branch — silently capping the concatenation again and
    reintroducing the very defect ([P2]) that split it, with the stale
    escalation tier swallowed and nothing red.

    Spec backlink: docs/decisions/DR-300-pickup-may-not-call-the-reconcile-orchestrator.md
    """
    cli_source = (
        Path(__file__).resolve().parents[3]
        / "coordinator" / "bin" / "workday-start-handoff-triage.py"
    ).read_text(encoding="utf-8")

    assert rht._AWAITING_GATE_SEPARATOR in cli_source, (
        f"{rht._AWAITING_GATE_SEPARATOR!r} no longer appears in the ported CLI — "
        "_cap_awaiting_gate_listing would fall through to the single-listing cap "
        "and silently swallow the >6d stale tier again"
    )
