"""
coordinator_core.tests.test_archival — Regression tests for
archival.reverse_membership's terminal-status / archive-resident child exclusion.

Bug: the referencedBy set returned by dag.referenced_by can include
archive-resident and/or terminal-status (consumed/superseded/abandoned)
children. A consumed parent whose only reverse-membership edge points at such
a child was reported as referenced=True forever, so it never archived — a
faithfully-ported bash bug (Port of: handoff-has-live-children.sh
(example-doctrine-repo 50ec0809, 2026-07-19) scanned both --type handoff and --type
handoff-archived into one undifferentiated set).

Fix: reverse_membership now excludes archive-resident and terminal-status
children from the returned live set — on a POSITIVE classification only.
Indeterminate frontmatter (unparseable, missing status) is retained
(fail-closed): only a definitively terminal status or definitive
archive-residency removes a child.

Tests:
    - test_terminal_child_excluded: parent whose only child is status:consumed
      (resident in state/handoffs/) → reverse_membership returns empty.
    - test_archived_child_excluded: parent whose only child resides under
      archive/handoffs/YYYY-MM/ (any status) → returns empty.
    - test_live_child_retained: parent whose child has status:active and is
      resident in state/handoffs/ → returns non-empty (still referenced).
    - test_indeterminate_child_retained_fail_closed: parent whose only child
      has unparseable/absent-status frontmatter but a valid edge → child is
      RETAINED (fail-closed), proving indeterminacy never excludes.
    - test_terminal_child_excluded_via_additional_predecessors: same exclusion
      as test_terminal_child_excluded, but the child's ONLY edge to the parent
      is the list-form additional_predecessors field, proving the exclusion
      predicate composes with the multi-valued edge kind (not just scalar
      predecessor).
    - test_consumed_in_flight_child_retained_as_live: regression for the
      2026-07-17 P2 false-archive vector (bug-backlog
      2026-07-17-archival-reverse-membership-ignores-deployment-state.yaml) —
      a status:consumed child with deployment_state:in_flight is still OPEN/
      unfinished work and MUST be retained (counted as live), not excluded.
    - test_consumed_not_in_flight_child_still_excluded: sanity companion to
      the above — a status:consumed child with deployment_state:shipped (or
      any non-in_flight value) is still excluded, proving the fix is scoped to
      in_flight only and doesn't regress the ordinary consumed-terminal case.
    - test_superseded_in_flight_child_still_excluded: proves the deployment_state
      carve-out is consumed-only — superseded/abandoned children remain
      terminal (excluded) unconditionally, even with deployment_state:in_flight.

Spec backlink: docs/plans/2026-07-02-pcore-03-beachhead-coordinator-core.md § C4
Bug-fix backlink: coordinator_core/archival.py::_is_terminal_or_archived_child
Memo: cross-repo/inbox/2026-07-09-claude-central-em-handoff-has-live-children-terminal-archived-exclusion.md
Bug-fix backlink (2026-07-17, deployment_state-aware terminal predicate):
  state/bug-backlog/2026-07-17-archival-reverse-membership-ignores-deployment-state.yaml
  state/review-trail/findings/2026-07-17-codereview-slicereconcile-open-dead-zone-coordinator-core-ops-handoff-reconcile-p.md
  (Finding 2)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coordinator_core.archival import reverse_membership


def _write_handoff(
    path: Path,
    *,
    status: str | None,
    predecessor: str,
    deployment_state: str | None = None,
) -> Path:
    """Write a minimal handoff file with the given frontmatter status and predecessor edge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    status_line = f"status: {status}" if status is not None else ""
    deployment_state_line = (
        f"deployment_state: {deployment_state}" if deployment_state is not None else ""
    )
    path.write_text(
        textwrap.dedent(f"""\
            ---
            session_id: session-arch-test
            goal: Archival test child
            {status_line}
            {deployment_state_line}
            predecessor: {predecessor}
            ---
            Child body.
        """),
        encoding="utf-8",
    )
    return path


def _write_parent(path: Path) -> Path:
    """Write a minimal parent handoff with no frontmatter status opinion needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent("""\
            ---
            session_id: session-arch-parent
            goal: Archival test parent
            ---
            Parent body.
        """),
        encoding="utf-8",
    )
    return path


def _write_handoff_additional_predecessors(
    path: Path, *, status: str | None, additional_predecessors: list[str]
) -> Path:
    """Write a handoff whose ONLY edge to the parent is the list-form
    additional_predecessors field (no scalar predecessor: line at all).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    status_line = f"status: {status}" if status is not None else ""
    inline_list = "[" + ", ".join(additional_predecessors) + "]"
    path.write_text(
        textwrap.dedent(f"""\
            ---
            session_id: session-arch-test
            goal: Archival test child
            {status_line}
            additional_predecessors: {inline_list}
            ---
            Child body.
        """),
        encoding="utf-8",
    )
    return path


# Review: code-reviewer F4 — flattened from single-method `class Test...:`
# wrappers to top-level functions, matching the flatter style of the sibling
# test_archive_handoffs.py.


def test_terminal_child_excluded(tmp_path: Path) -> None:
    """A consumed child resident in state/handoffs/ is excluded from the live set."""
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md", status="consumed", predecessor=str(parent)
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"terminal (consumed) child must be excluded from live set; got {result}"
    )


def test_archived_child_excluded(tmp_path: Path) -> None:
    """A child resident under archive/handoffs/YYYY-MM/ is excluded regardless of status."""
    state_dir = tmp_path / "state" / "handoffs"
    archive_dir = tmp_path / "archive" / "handoffs" / "2026-06"
    parent = _write_parent(state_dir / "parent.md")
    # status:active on purpose — archive-residency alone must be sufficient to exclude.
    child = _write_handoff(
        archive_dir / "child.md", status="active", predecessor=str(parent)
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"archive-resident child must be excluded from live set regardless of "
        f"status; got {result}"
    )


def test_live_child_retained(tmp_path: Path) -> None:
    """A genuinely live (active status, state/-resident) child is still counted."""
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md", status="active", predecessor=str(parent)
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    # Review: code-reviewer F3 — dropped the `or len(result) > 0` clause, which
    # made the membership check vestigial (a bug returning the wrong path but
    # right count would have slipped through). Assert membership directly.
    assert str(Path(child).resolve()) in {str(Path(c).resolve()) for c in result}, (
        f"live child must be retained (referenced=True); got {result}"
    )
    assert len(result) == 1, f"expected exactly one live child; got {result}"


def test_indeterminate_child_retained_fail_closed(tmp_path: Path) -> None:
    """A child with unparseable/absent-status frontmatter is retained (fail-closed)."""
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    # No status: line at all — frontmatter parses but status key is absent.
    child = _write_handoff(
        state_dir / "child.md", status=None, predecessor=str(parent)
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert len(result) == 1, (
        f"indeterminate (absent-status) child must be RETAINED (fail-closed), "
        f"not excluded; got {result}"
    )


def test_terminal_child_excluded_via_additional_predecessors(tmp_path: Path) -> None:
    """Review: code-reviewer F5 — the terminal/archived exclusion must compose with
    the multi-valued additional_predecessors edge kind, not just the scalar
    predecessor field. A consumed child whose ONLY edge to the parent is
    additional_predecessors: [<parent>] (list form) must still be excluded.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff_additional_predecessors(
        state_dir / "child.md",
        status="consumed",
        additional_predecessors=[str(parent)],
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"terminal (consumed) child reachable only via additional_predecessors "
        f"must be excluded from live set; got {result}"
    )


def test_consumed_in_flight_child_retained_as_live(tmp_path: Path) -> None:
    """Regression for the 2026-07-17 P2 false-archive vector: a consumed child
    with deployment_state:in_flight is still OPEN/unfinished work (the exact
    archive-complement of ops/fleet/archive_handoffs.py's Check A2 / DR-215
    lockstep) and MUST NOT be excluded from the live set. Without the fix,
    a grandparent/parent naming this child's predecessor edge would be
    incorrectly reported as having zero live children — a false-archive vector
    for fleet.archive_completed_handoffs' Check 3 consumed-sweep.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="consumed",
        deployment_state="in_flight",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert str(Path(child).resolve()) in {str(Path(c).resolve()) for c in result}, (
        f"consumed+in_flight child is still OPEN work and must be retained "
        f"(counted as live), not excluded; got {result}"
    )
    assert len(result) == 1, f"expected exactly one live child; got {result}"


def test_consumed_not_in_flight_child_still_excluded(tmp_path: Path) -> None:
    """Sanity companion to test_consumed_in_flight_child_retained_as_live: a
    consumed child with a non-in_flight deployment_state (e.g. shipped) is
    still excluded — the fix is scoped to in_flight only, not a blanket
    consumed-status carve-out.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="consumed",
        deployment_state="shipped",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"consumed child with non-in_flight deployment_state must remain "
        f"excluded from live set; got {result}"
    )


def test_open_status_closed_deployment_state_child_excluded(tmp_path: Path) -> None:
    """DR-084 regression: a `status: open` child with a terminal
    `deployment_state: closed` (the close-handoff verb's shape — status is
    left `open`, only deployment_state and closed_reason are stamped) must be
    excluded from the live set. Live reproduction prior to this fix:
    state/handoffs/2026-07-10_141606_roadmap-qsub-03.md carries exactly this
    shape and was permanently counted as live because the old predicate never
    consulted deployment_state as a positive rule.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="open",
        deployment_state="closed",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"status:open + deployment_state:closed child must be excluded from "
        f"the live set (DR-084 terminal-deployment-state rule); got {result}"
    )


def test_open_status_shipped_deployment_state_child_excluded(tmp_path: Path) -> None:
    """Same DR-084 shape as above, with deployment_state:shipped."""
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="open",
        deployment_state="shipped",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"status:open + deployment_state:shipped child must be excluded from "
        f"the live set; got {result}"
    )


def test_open_status_continued_deployment_state_child_excluded(tmp_path: Path) -> None:
    """Same DR-084 shape as above, with deployment_state:continued."""
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="open",
        deployment_state="continued",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"status:open + deployment_state:continued child must be excluded "
        f"from the live set; got {result}"
    )


def test_claimed_in_flight_child_retained_despite_terminal_deployment_rule(
    tmp_path: Path,
) -> None:
    """DR-084 carve-out interaction check: a status:claimed child with
    deployment_state:in_flight must still be RETAINED — `in_flight` is not a
    member of HANDOFF_TERMINAL_DEPLOYMENT, so the new rule 3 must not fire
    and must not resurrect the case rule 2's carve-out deliberately retains.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="claimed",
        deployment_state="in_flight",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert str(Path(child).resolve()) in {str(Path(c).resolve()) for c in result}, (
        f"claimed+in_flight child is still OPEN work and must be retained "
        f"(counted as live), not excluded; got {result}"
    )
    assert len(result) == 1, f"expected exactly one live child; got {result}"


def test_absent_deployment_state_still_retained_fail_closed(tmp_path: Path) -> None:
    """Fail-closed sanity: an absent deployment_state (no key at all) with
    status:open must not be excluded by the new terminal-deployment-state
    rule — only a definitive terminal value fires it.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="open",
        deployment_state=None,
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert str(Path(child).resolve()) in {str(Path(c).resolve()) for c in result}, (
        f"status:open + absent deployment_state child must be RETAINED "
        f"(fail-closed), not excluded; got {result}"
    )
    assert len(result) == 1, f"expected exactly one live child; got {result}"


def test_unrecognized_deployment_state_still_retained_fail_closed(tmp_path: Path) -> None:
    """Fail-closed sanity: an unrecognized deployment_state value must not be
    excluded by the new terminal-deployment-state rule.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="open",
        deployment_state="some-unrecognized-value",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert str(Path(child).resolve()) in {str(Path(c).resolve()) for c in result}, (
        f"status:open + unrecognized deployment_state child must be RETAINED "
        f"(fail-closed), not excluded; got {result}"
    )
    assert len(result) == 1, f"expected exactly one live child; got {result}"


def test_superseded_in_flight_child_still_excluded(tmp_path: Path) -> None:
    """The deployment_state:in_flight carve-out is consumed-only: a superseded
    child is terminal (excluded) unconditionally, even with
    deployment_state:in_flight — proving the fix does not widen to the other
    _TERMINAL_STATUSES members.
    """
    state_dir = tmp_path / "state" / "handoffs"
    parent = _write_parent(state_dir / "parent.md")
    child = _write_handoff(
        state_dir / "child.md",
        status="superseded",
        deployment_state="in_flight",
        predecessor=str(parent),
    )

    result = reverse_membership(str(parent), [str(parent), str(child)])

    assert result == frozenset(), (
        f"superseded child must remain excluded regardless of deployment_state; "
        f"got {result}"
    )
