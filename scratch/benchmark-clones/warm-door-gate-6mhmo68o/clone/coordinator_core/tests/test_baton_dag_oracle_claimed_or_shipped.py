"""coordinator_core.tests.test_baton_dag_oracle_claimed_or_shipped — DR-242 predicate.

Coverage: `_baton_dag_oracle.claimed_or_shipped` (pure, frontmatter-string-in ->
bool-out) and `claimed_or_shipped_at_path` (path-based wrapper) across both
claimed-vocabulary generations (DR-084 rename, commit 92c90205: claimed_at/
claimed_by is current, consumed_at/consumed_by is the retired-but-still-read
archived vocabulary), the terminal-`deployment_state` axis, the `shipped_in`
axis, and the negative case a never-claimed, never-shipped baton with only a
successor-named child's naming convention pointing at it -- the exact
discriminator hazard DR-242 forbids treating as sufficient (§ 3 of that DR).

Spec backlink: DR-242, C5a of pln-handoff-close-path-fail-loud-b-db23e8.
"""
from __future__ import annotations

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.tests._baton_dag_oracle import (
    _TERMINAL_DEPLOYMENT_STATES,
    claimed_or_shipped,
    claimed_or_shipped_at_path,
)


def _fm(**fields: str) -> str:
    return "\n".join(f"{k}: {v}" for k, v in fields.items())


def test_never_claimed_never_shipped_is_false():
    fm = _fm(title="t", status="open", deployment_state="awaiting_gate")
    assert claimed_or_shipped(fm) is False


def test_successor_named_child_alone_is_not_evidence():
    """DR-242's own negative case: this predicate inspects ONLY the candidate
    parent's own frontmatter -- a child's predecessor:/predecessor_id: pointer
    lives in the CHILD's frontmatter, never the parent's, so it can never leak
    into this function's input in the first place."""
    parent_fm = _fm(title="never picked up", status="open", deployment_state="awaiting_gate")
    assert claimed_or_shipped(parent_fm) is False


def test_status_claimed_new_vocabulary_is_true():
    assert claimed_or_shipped(_fm(status="claimed")) is True


def test_status_consumed_retired_vocabulary_is_true():
    assert claimed_or_shipped(_fm(status="consumed")) is True


def test_status_superseded_retired_vocabulary_is_true():
    """Review: code-reviewer, Finding 2/4 — `superseded` is a documented
    read-tolerance archived status (handoff-archived.schema.json's `status`
    enum), retired 2026-06-26 on the same fleet-shared archived/legacy
    read-tolerance grounds as `consumed`. A record whose status is literally
    `superseded` is by definition already claimed-or-shipped."""
    assert claimed_or_shipped(_fm(status="superseded")) is True


def test_claimed_at_field_alone_is_true():
    assert claimed_or_shipped(_fm(claimed_at="2026-07-28T00:00:00Z")) is True


def test_claimed_by_field_alone_is_true():
    assert claimed_or_shipped(_fm(claimed_by="session-abc")) is True


def test_consumed_at_field_retired_vocabulary_alone_is_true():
    assert claimed_or_shipped(_fm(consumed_at="2026-07-28T00:00:00Z")) is True


def test_consumed_by_field_retired_vocabulary_alone_is_true():
    assert claimed_or_shipped(_fm(consumed_by="session-abc")) is True


def test_deployment_state_shipped_is_true():
    assert claimed_or_shipped(_fm(deployment_state="shipped")) is True


def test_deployment_state_continued_is_true():
    assert claimed_or_shipped(_fm(deployment_state="continued")) is True


def test_deployment_state_closed_is_true():
    assert claimed_or_shipped(_fm(deployment_state="closed")) is True


def test_deployment_state_abandoned_is_true():
    """`abandoned` is the DR-084 OLD deployment-state term, still carried by
    archived and consumer-repo corpora this predicate reads (that is precisely
    what `lifecycle_constants`' dual-vocabulary read tolerance, restored at
    9d00b459, exists for). A record carrying it was terminally disposed of.

    Regression: the oracle hand-listed the schema's post-P4 WRITE enum tail
    ("shipped"/"continued"/"closed") and so answered False here, making it a
    genuine differential-oracle divergence — an abandoned parent read
    non-terminal in the oracle and terminal at every SSOT-derived production
    site, so DR-242's gate refused a legitimate supersede against it."""
    assert claimed_or_shipped(_fm(deployment_state="abandoned")) is True


def test_terminal_deployment_states_is_derived_from_the_ssot():
    """The oracle's terminal set is DERIVED, not hand-listed — the drift that
    produced the `abandoned` divergence cannot recur silently."""
    assert set(_TERMINAL_DEPLOYMENT_STATES) == set(HANDOFF_TERMINAL_DEPLOYMENT)


def test_abandoned_parent_is_terminal_for_oracle_and_production_alike(tmp_path):
    """Differential assertion proper: on the same on-disk record, the oracle's
    read-side verdict and production's SSOT-derived terminal predicates agree
    that an `abandoned` parent is terminal.

    `ops.fleet._common` and `write_guards.block_consumed_handoff_edit` are the
    production sites that answer this same read-side question (both alias
    HANDOFF_TERMINAL_DEPLOYMENT); the oracle is checked through its own
    from-scratch path (`claimed_or_shipped_at_path` -> `_frontmatter`), not by
    delegating to either of them."""
    from coordinator_core.ops.fleet._common import (
        _TERMINAL_DEPLOYMENT_STATES as _PRODUCTION_TERMINAL,
    )
    from coordinator_core.write_guards.block_consumed_handoff_edit import (
        _TERMINAL_DEPLOYMENT_STATES as _GUARD_TERMINAL,
    )

    handoff = tmp_path / "hnd.md"
    handoff.write_text(
        "---\ntitle: t\nstatus: open\ndeployment_state: abandoned\n---\nbody\n",
        encoding="utf-8",
    )

    assert claimed_or_shipped_at_path(str(handoff)) is True
    assert "abandoned" in _PRODUCTION_TERMINAL
    assert "abandoned" in _GUARD_TERMINAL


def test_deployment_state_in_flight_alone_is_false():
    """in_flight is a live, non-terminal state; without a claimed_at/claimed_by
    or a claimed/consumed status it does not, on its own, prove pickup."""
    assert claimed_or_shipped(_fm(deployment_state="in_flight")) is False


def test_shipped_in_field_alone_is_true():
    assert claimed_or_shipped(_fm(shipped_in="a1b2c3d")) is True


def test_shipped_in_none_literal_is_false():
    assert claimed_or_shipped(_fm(shipped_in="none")) is False


def test_empty_frontmatter_is_false():
    assert claimed_or_shipped("") is False


def test_at_path_unreadable_file_fails_closed(tmp_path):
    missing = tmp_path / "does-not-exist.md"
    assert claimed_or_shipped_at_path(str(missing)) is False


def test_at_path_reads_real_frontmatter(tmp_path):
    handoff = tmp_path / "hnd.md"
    handoff.write_text(
        "---\ntitle: t\nstatus: claimed\nclaimed_by: sess-1\n---\nbody\n",
        encoding="utf-8",
    )
    assert claimed_or_shipped_at_path(str(handoff)) is True


def test_at_path_never_claimed_real_frontmatter(tmp_path):
    handoff = tmp_path / "hnd.md"
    handoff.write_text(
        "---\ntitle: t\nstatus: open\ndeployment_state: awaiting_gate\n---\nbody\n",
        encoding="utf-8",
    )
    assert claimed_or_shipped_at_path(str(handoff)) is False


def test_at_path_reads_frontmatter_behind_a_leading_preamble(tmp_path):
    """A claimed handoff whose frontmatter sits behind an HTML-comment
    preamble must read as claimed.

    `_frontmatter` previously required the file to literally start with
    `---` and returned "" otherwise, so a preamble-carrying handoff looked
    like it had no frontmatter at all — and this predicate, unable to see
    any `status`, concluded "never claimed or shipped". Because five
    production gates import it (archive_stamp, baton_assemble.apply,
    ops/handoff_transition, ops/handoff_archive_transition, and
    bin/handoff-archive-transition.py), DR-242 would REFUSE a supersede on
    a handoff that had in fact been claimed, reporting the opposite of the
    truth. A preamble is a supported shape — the frontmatter parity suite
    covers it and production's split_frontmatter parses it.
    """
    handoff = tmp_path / "hnd.md"
    handoff.write_text(
        "<!-- coordinator: generated 2026-01-01 -->\n"
        "---\ntitle: t\nstatus: claimed\nclaimed_by: sess-1\n---\nbody\n",
        encoding="utf-8",
    )
    assert claimed_or_shipped_at_path(str(handoff)) is True


def test_at_path_body_prose_before_a_horizontal_rule_is_not_frontmatter(tmp_path):
    """The preamble skip stays narrow: only blank lines and HTML comments
    may precede the opening `---`. A document whose body prose happens to
    contain a `---` horizontal rule must still read as having NO
    frontmatter, rather than having the rule mistaken for an opening
    fence — otherwise the preamble tolerance would turn into a
    false-positive machine pointed at every markdown file in the tree."""
    doc = tmp_path / "notes.md"
    doc.write_text("just prose\n\n---\n\nstatus: claimed\n", encoding="utf-8")
    assert claimed_or_shipped_at_path(str(doc)) is False
