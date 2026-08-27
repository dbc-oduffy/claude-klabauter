"""
coordinator_core.reconcile.tests.test_continuation_chain_collision

Purpose: a `stub_id` names an entire CONTINUATION CHAIN, not one record, and the two
independent id resolvers must both collapse that chain to its head before deciding
anything. They previously disagreed in opposite directions on the same corpus, and
both were wrong:

  * compute-time (`gate_eval._index_by_id`) resolved the stub to whichever record its
    caller's walker appended LAST. `_collect_all_handoffs_for_gate_index` appends the
    archived half after the live half, so a superseded record beat the live head; the
    head's real `blocks:` list was then read off a record authored `blocks: []` and
    `_has_asymmetry` reported a symmetric graph as a data defect.
  * act-time (`handoff_transition._resolve_blocker_deployment_state`) saw
    `len(matches) > 1` and returned `<ambiguous-duplicate-id>`, so
    `_blocker_clears_gate` answered False forever and `_gate_cascade_clear` could
    never clear that blocker's dependents no matter what shipped.

Live corpus that produced both: `ceremony-restore-01`, 10 records (5 live, 5 archived).

NEGATIVE SPEC — what these tests deliberately pin as UNCHANGED:
  * a genuine cross-family `stub_id` collision (two unrelated records, no continuation
    edge between them) still collides at compute time and still fails loud as ambiguous
    at act time. The collapse narrows what counts as a competing claim; it does not
    relax either caller's posture toward one.
  * a group in which EVERY record is superseded resolves to the group, never to
    nothing — a fully-continued blocker is resolvable via `continued_into` and must not
    be turned into a dangling ref.
"""

from pathlib import Path

from coordinator_core.reconcile.gate_eval import (
    _has_asymmetry,
    _index_by_id,
    collapse_to_chain_heads,
)


def _record(path, *, stub_id="stub-01", state="in_flight", **fields):
    record = {"stub_id": stub_id, "deployment_state": state, "_path": path}
    record.update(fields)
    return record


# --------------------------------------------------------------------------
# collapse_to_chain_heads — the shared primitive
# --------------------------------------------------------------------------


def test_stamped_supersession_loses_to_the_live_head():
    """Signal (1)/(2): `deployment_state: continued` + `continued_into`."""
    superseded = _record(
        "archive/handoffs/2026-08/a.md",
        state="continued",
        continued_into="state/handoffs/b.md",
        blocks=[],
    )
    head = _record("state/handoffs/b.md", blocks=["dep-01"])

    assert collapse_to_chain_heads([head, superseded]) == [head]


def test_unstamped_predecessor_loses_to_the_successor_that_names_it():
    """Signal (3), and the one that makes the collapse total.

    The successor's `predecessor` field is written when the successor is MINTED; the
    predecessor's own `continued` stamp is a separate later step that is not always
    reached. 4 of the 10 records in the corpus above carried a successor naming them
    while carrying no supersession stamp of their own — trusting only the stamp left
    all four competing with the real head.
    """
    unstamped = _record("state/handoffs/a.md", state="shipped", blocks=["dep-01"])
    head = _record(
        "state/handoffs/b.md",
        predecessor="state/handoffs/a.md",
        blocks=["dep-01"],
    )

    assert collapse_to_chain_heads([unstamped, head]) == [head]


def test_fan_in_legs_are_superseded_by_the_leg_that_names_them():
    """`additional_predecessors` carries the fan-in legs beyond the primary."""
    leg_a = _record("state/handoffs/a.md", state="shipped")
    leg_b = _record("state/handoffs/b.md", state="shipped")
    head = _record(
        "state/handoffs/c.md",
        predecessor="state/handoffs/a.md",
        additional_predecessors=["state/handoffs/b.md"],
    )

    assert collapse_to_chain_heads([leg_a, leg_b, head]) == [head]


def test_predecessor_none_sentinel_supersedes_nothing():
    """`coordinator-doc-new` emits the literal `predecessor: none` when the flag is
    not passed. Treating it as a path would index a `none` basename and, worse, read
    as a real up-edge."""
    only = _record("state/handoffs/none", predecessor="none")

    assert collapse_to_chain_heads([only]) == [only]


def test_a_fully_superseded_group_returns_the_group_not_nothing():
    """Every record continued is a LEGITIMATE shape — the successor lives under a
    different `stub_id`, and `continued_into` is how both callers reach it. Collapsing
    to empty would turn a resolvable blocker into a dangling ref."""
    a = _record("state/handoffs/a.md", state="continued", continued_into="x.md")
    b = _record("state/handoffs/b.md", state="continued", continued_into="y.md")

    assert collapse_to_chain_heads([a, b]) == [a, b]


def test_a_genuine_collision_survives_the_collapse_undecided():
    """Two unrelated records sharing an unprefixed `stub_id`, no continuation edge
    between them: NOT this function's job to disambiguate, and it does not try."""
    a = _record("state/handoffs/family-a.md")
    b = _record("state/handoffs/family-b.md")

    assert collapse_to_chain_heads([a, b]) == [a, b]


def test_records_with_no_path_field_fall_back_to_the_stamp():
    """Signal (3) needs a path-shaped field to match on; (1)/(2) still apply without
    one. A caller whose collector attaches no path must not lose the stamped case."""
    superseded = {"stub_id": "s", "deployment_state": "continued", "continued_into": "b.md"}
    head = {"stub_id": "s", "deployment_state": "in_flight"}

    assert collapse_to_chain_heads([superseded, head]) == [head]


# --------------------------------------------------------------------------
# compute-time — _index_by_id / _has_asymmetry
# --------------------------------------------------------------------------


def test_index_resolves_the_stub_to_the_live_head_not_the_last_appended():
    """The exact ordering that produced the defect: archived-and-superseded appended
    AFTER the live head, which is what `_collect_all_handoffs_for_gate_index` does."""
    head = _record(
        "state/handoffs/head.md",
        blocks=["dep-01"],
        predecessor="archive/handoffs/2026-08/old.md",
    )
    superseded = _record(
        "archive/handoffs/2026-08/old.md",
        state="continued",
        continued_into="state/handoffs/head.md",
        blocks=[],
    )

    index = _index_by_id([head, superseded])

    assert index.get("stub-01") is head


def test_symmetric_edge_on_the_head_stops_reading_as_asymmetry():
    """End-to-end at compute time: the dependent's gate check against a chain whose
    head DOES name it back. Before the collapse this returned True — a symmetric
    graph reported as `blocks/blocked_by asymmetry detected — data defect`."""
    head = _record(
        "state/handoffs/head.md",
        kind="roadmap-baton",
        blocks=["dep-01"],
        predecessor="archive/handoffs/2026-08/old.md",
    )
    superseded = _record(
        "archive/handoffs/2026-08/old.md",
        kind="roadmap-baton",
        state="continued",
        continued_into="state/handoffs/head.md",
        blocks=[],
    )
    dependent = {"stub_id": "dep-01", "blocked_by": ["stub-01"]}

    index = _index_by_id([head, superseded])

    assert _has_asymmetry(dependent, ["stub-01"], index) is False


def test_a_genuinely_severed_edge_still_reports_asymmetry():
    """The collapse must not make `_has_asymmetry` unable to fire. A head that really
    does not name the dependent back is still a data defect."""
    head = _record("state/handoffs/head.md", kind="roadmap-baton", blocks=[])
    dependent = {"stub_id": "dep-01", "blocked_by": ["stub-01"]}

    index = _index_by_id([head])

    assert _has_asymmetry(dependent, ["stub-01"], index) is True


# --------------------------------------------------------------------------
# act-time — _resolve_blocker_deployment_state
# --------------------------------------------------------------------------


_FM = """---
title: {title}
stub_id: "{stub_id}"
kind: roadmap-baton
deployment_state: {state}
{extra}---

body
"""


def _write(path: Path, *, stub_id, state, extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _FM.format(title=path.stem, stub_id=stub_id, state=state, extra=extra),
        encoding="utf-8",
    )


def test_act_time_resolves_a_continuation_chain_to_its_head(tmp_path):
    """Was `<ambiguous-duplicate-id>` — a permanent `_gate_cascade_clear` wedge
    presenting as an integrity guard."""
    from coordinator_core.ops.handoff_transition import _resolve_blocker_deployment_state

    _write(
        tmp_path / "archive" / "handoffs" / "2026-08" / "old.md",
        stub_id="chain-01",
        state="continued",
        extra="continued_into: state/handoffs/head.md\n",
    )
    _write(
        tmp_path / "state" / "handoffs" / "mid.md",
        stub_id="chain-01",
        state="shipped",
    )
    _write(
        tmp_path / "state" / "handoffs" / "head.md",
        stub_id="chain-01",
        state="shipped",
        extra="predecessor: state/handoffs/mid.md\n",
    )

    resolved = _resolve_blocker_deployment_state("chain-01", tmp_path)

    assert resolved.deployment_state == "shipped"


def test_act_time_still_fails_loud_on_a_genuine_duplicate_id(tmp_path):
    """The ambiguity guard is narrowed, never relaxed: two records sharing a
    `stub_id` with no continuation edge between them remain unresolvable."""
    from coordinator_core.ops.handoff_transition import (
        _AMBIGUOUS_BLOCKER_SENTINEL,
        _resolve_blocker_deployment_state,
    )

    _write(tmp_path / "state" / "handoffs" / "family-a.md", stub_id="dup-01", state="shipped")
    _write(tmp_path / "state" / "handoffs" / "family-b.md", stub_id="dup-01", state="in_flight")

    resolved = _resolve_blocker_deployment_state("dup-01", tmp_path)

    assert resolved.deployment_state == _AMBIGUOUS_BLOCKER_SENTINEL


def test_act_time_and_compute_time_agree_on_the_same_chain(tmp_path):
    """The two resolvers exist to answer the same question and previously disagreed.
    Pin that they now agree on one corpus, so a future change to either that
    reintroduces the split fails here rather than in a wedged ceremony."""
    from coordinator_core.ops.handoff_transition import _resolve_blocker_deployment_state
    from coordinator_core.reconcile.handoff_corpus import (
        _collect_all_handoffs_for_gate_index,
    )

    _write(
        tmp_path / "archive" / "handoffs" / "2026-08" / "old.md",
        stub_id="agree-01",
        state="continued",
        extra="continued_into: state/handoffs/head.md\n",
    )
    _write(
        tmp_path / "state" / "handoffs" / "head.md",
        stub_id="agree-01",
        state="shipped",
        extra="predecessor: archive/handoffs/2026-08/old.md\n",
    )

    all_handoffs, _ = _collect_all_handoffs_for_gate_index(tmp_path)
    compute_time = _index_by_id(all_handoffs).get("agree-01")
    act_time = _resolve_blocker_deployment_state("agree-01", tmp_path)

    assert compute_time is not None
    assert compute_time.get("deployment_state") == act_time.deployment_state == "shipped"
