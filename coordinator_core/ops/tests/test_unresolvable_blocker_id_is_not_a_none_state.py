"""
coordinator_core.ops.tests.test_unresolvable_blocker_id_is_not_a_none_state

Purpose: a blocker id that resolves to NO record must be reported as a dangling
reference, not as a record whose `deployment_state` happens to be `None`. The two
are different defects with different remedies -- a dangling id is a data error in
the dependent's `blocked_by` (fix the reference), while a null deployment_state
would be a malformed blocker record (fix the blocker) -- and today's refusal text
flattens them into one string an operator cannot act on.

`_resolve_blocker_deployment_state` already distinguishes them internally: it
returns `_UNRESOLVED_BLOCKER_STATE` for a no-match, structurally distinct from the
`_AMBIGUOUS_BLOCKER_SENTINEL` it returns for a surviving multi-head group. The
information is present and is discarded one frame later, when
`_blocker_clears_gate` falls through to its catch-all
`f"{current_id!r} live deployment_state: {ds!r}"`.

Live case this was found on: `rethink-refactor-kill-the-composition-hot-paths`
carries `blocked_by` entry `roadmap-op-proportionality`, which matches no record in
`state/handoffs/` or `archive/handoffs/` by either `stub_id` or `handoff_id`. The
gate refusal it produces reads `live deployment_state: None`, which describes a
record that does not exist.

Spec backlink: state/sizings/2026-08-27-the-gate-resolver-cannot-name-a-blocker.yaml
(`spike_amendments[]`, scope "RESIDUAL"), and
docs/research/spike-verdicts/2026-08-27-the-blocker-id-resolves-to-a-lineage-terminus.md.

Negative-spec: this is NOT a request to make an unresolvable blocker CLEAR its
gate. It keeps refusing -- an id naming nothing is never evidence that the
blocked-on work landed. Only the operator-facing reason changed. Nor does it touch
the ambiguity sentinel, which guards a different failure (glob-sort order deciding
a lifecycle verdict) and stays exactly as loud as it is.

Fixed by giving `_BlockerState` an explicit `resolved` flag rather than inferring
the case from a `None` deployment_state -- a matched record carrying no
`deployment_state` produces the same `None` and is a different defect.
"""

from __future__ import annotations

from pathlib import Path


from coordinator_core.ops.handoff_transition import (
    _blocker_clears_gate,
    _resolve_blocker_deployment_state,
)


def _write_handoff(worktree: Path, name: str, **fields: object) -> None:
    """Write one minimal handoff record into the live handoffs root."""
    root = worktree / "state" / "handoffs"
    root.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.extend(f"{key}: {value!r}" for key, value in fields.items())
    lines.extend(["---", "", f"# {name}", ""])
    (root / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


def test_unresolvable_id_refusal_does_not_claim_a_none_deployment_state(
    tmp_path: Path,
) -> None:
    """An id matching no record must not be described by a deployment_state."""
    _write_handoff(
        tmp_path,
        "some-other-baton",
        stub_id="some-other-baton",
        deployment_state="in_flight",
    )

    state = _resolve_blocker_deployment_state("names-nothing-at-all", tmp_path)
    assert state.deployment_state is None, (
        "precondition: the resolver already represents a no-match distinctly"
    )

    clears, detail = _blocker_clears_gate("names-nothing-at-all", tmp_path)
    assert clears is False, "an unresolvable id must never clear a gate"
    assert "deployment_state" not in detail, (
        "refusal describes a deployment_state for a record that does not exist: "
        f"{detail!r}"
    )
    # Assert what the message DOES say, not only what it no longer says: a rename
    # that reintroduced the old catch-all under a new field name would satisfy the
    # negative assertion alone.
    assert "no handoff record" in detail, (
        f"refusal does not name the dangling reference: {detail!r}"
    )
    assert "names-nothing-at-all" in detail, "refusal must name the offending id"


def test_a_chain_going_dangling_mid_hop_names_the_hop_not_the_origin(
    tmp_path: Path,
) -> None:
    """The chase re-resolves per hop, so the verdict must attribute to the hop.

    A chain that starts real and goes dangling partway is the case that separates
    "this blocker id is bad" from "this blocker's successor pointer is bad" — two
    different records to go and fix. `_blocker_clears_gate` re-reads disk on every
    hop, so `current_id` (not the original `blocker_id`) is what the refusal must
    name.
    """
    _write_handoff(
        tmp_path,
        "real-origin",
        stub_id="real-origin",
        deployment_state="continued",
        continued_into="no-such-successor",
    )

    clears, detail = _blocker_clears_gate("real-origin", tmp_path)

    assert clears is False
    assert "no-such-successor" in detail, (
        f"refusal blames the origin instead of the dangling hop: {detail!r}"
    )
    assert "no handoff record" in detail
