"""
coordinator_core.tests.test_tracker_projection — C3 fold/wire-shape tests.

Purpose: cover the membership fold (`fold_membership`) and the wire-shape
helper (`fold_membership_wire`) in `coordinator_core.tracker_projection`.

Coverage requirements (plan docs/plans/2026-08-05-sat-02-sovereign-tracker-
relational-spine.md § Acceptance Criteria, § Tasks C3):
  AC3  — fold totality: EVERY item reachable in the event stream folds to a
         non-empty membership set, not just the hand-constructed AC4/AC5
         cases. This is the property cockpit actually asked us to test
         (DEC-21) — mutual exclusivity is now unrepresentable and is
         deliberately NOT re-tested here as if it could still fail.
  AC4  — zero real edges folds to exactly {unassigned}.
  AC5  — >=1 real edge folds to exactly those edges, never unassigned;
         retracting a non-last edge leaves the remainder; retracting the
         LAST real edge re-folds to {unassigned} with no extra write.
  AC6  — two concurrent cross-process membership mutations on the same item
         both land; the projection is the deterministic fold of both (no
         lost update, no interleaved artifact) — models the ccos-4
         lost-update shape (docs/plans/2026-07-06-cross-process-rmw-file-
         locking.md), following the sleep-widened subprocess technique in
         coordinator_core/tests/test_tracker_store.py's AC1.
  AC12 — no projection logic lives in tracker_store.read_events.
  AC16 — the reserved unassigned project row is addressable whether or not
         any item folds to it.
  AC18 — the wire helper emits materialized projects[] with "unassigned"
         present for a zero-real-edge item; never an empty array, never raw
         events.
  AC9  — (C4) item_person admits (item, person, assignee) and (item, person,
         raised_by) simultaneously, over DEC-18's three-part natural key,
         and rejects an exact duplicate triple at emission time.

Spec backlink: docs/plans/2026-08-05-sat-02-sovereign-tracker-relational-
spine.md § Acceptance Criteria, § Tasks C3/C4.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core import tracker_entities, tracker_store
from coordinator_core.tracker_entities import (
    RESERVED_PROJECT_ID,
    TrackerEntityError,
    emit_item_created,
    emit_item_person_added,
    emit_item_person_retracted,
    emit_item_project_added,
    emit_item_project_retracted,
    emit_project_created,
    item_project_added,
    item_project_retracted,
    mint_item_id,
)
from coordinator_core.tracker_projection import (
    fold_membership,
    fold_membership_wire,
    fold_person_membership,
)

_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _make_git_repo(root: Path) -> Path:
    """Init a minimal git repository under *root* — ``append_event``'s
    ``locked_rmw`` resolves its lock directory via ``git rev-parse
    --git-common-dir``, so a bare non-git ``tmp_path`` fails before any of
    this chunk's own logic runs. Mirrors ``test_tracker_store.py``'s
    ``_make_git_repo`` exactly."""
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-projection-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Projection Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


@pytest.fixture
def repo_root(tmp_path):
    return _make_git_repo(tmp_path / "repo")


def _emit_membership_event_for_foreign_item(item_id, project_id, *, kind, repo_root):
    """Append a raw `item_project_added`/`item_project_retracted` event for
    an *item_id* with no local `item_created` event — bypassing
    `emit_item_project_added`/`_retracted`'s DEC-24 `_require_local_item`
    guard, which exists to refuse exactly this at the emission API surface.
    Simulates data `fold_membership` must still fold correctly regardless
    of provenance (e.g. an item created in a peer repo and replicated in),
    since the guard lives in `tracker_entities`, not in the read/fold path.
    """
    payload = (
        item_project_added(item_id, project_id)
        if kind == "item_project_added"
        else item_project_retracted(item_id, project_id)
    )
    return tracker_entities._emit(
        payload, item_id_or_pair=(item_id, project_id), repo_root=repo_root
    )


def _make_item(repo_root, *, title="Widget", body="Do the thing"):
    item_id = mint_item_id(title, body, "2026-08-05T10:00:00.000000Z")
    emit_item_created(
        item_id,
        title=title,
        body=body,
        created_at="2026-08-05T10:00:00.000000Z",
        repo_root=repo_root,
    )
    return item_id


# ---------------------------------------------------------------------------
# AC3 — fold totality over every item reachable in the stream
# ---------------------------------------------------------------------------


def test_ac3_every_item_reachable_in_the_stream_folds_non_empty(repo_root):
    # Deliberately a mixed population: a bare item_created with zero
    # project mutations at all, an item with a real edge, and an item
    # toggled back to zero edges — every one of them must still be a
    # member of the fold with a non-empty set.
    bare_item = _make_item(repo_root, title="Bare", body="no project mutation ever")
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)

    with_edge = _make_item(repo_root, title="WithEdge", body="has a real project")
    emit_item_project_added(with_edge, "proj-alpha", repo_root=repo_root)

    toggled_to_empty = _make_item(repo_root, title="Toggled", body="added then retracted")
    emit_item_project_added(toggled_to_empty, "proj-alpha", repo_root=repo_root)
    emit_item_project_retracted(toggled_to_empty, "proj-alpha", repo_root=repo_root)

    # Review: code-reviewer (Finding 1) — an item known to the stream ONLY
    # via an item_project_added event, with no item_created at all. This
    # exercises fold_membership's `elif` branch seeding an item on its own,
    # independent of the item_created branch (see module docstring).
    only_added = mint_item_id("OnlyAdded", "no item_created ever", "2026-08-05T10:00:00.000000Z")
    _emit_membership_event_for_foreign_item(
        only_added, "proj-alpha", kind="item_project_added", repo_root=repo_root
    )

    # Review: code-reviewer (Finding 1) — an item known to the stream ONLY
    # via an item_project_retracted event, no prior add and no item_created.
    only_retracted = mint_item_id(
        "OnlyRetracted", "no prior add, no item_created", "2026-08-05T10:00:00.000000Z"
    )
    _emit_membership_event_for_foreign_item(
        only_retracted, "proj-alpha", kind="item_project_retracted", repo_root=repo_root
    )

    folded = fold_membership(repo_root=repo_root)

    for item_id in (bare_item, with_edge, toggled_to_empty, only_added, only_retracted):
        assert item_id in folded, f"{item_id} vanished from the fold entirely"
        assert folded[item_id], f"{item_id} folded to the empty set — totality violated"


# ---------------------------------------------------------------------------
# AC4 — zero real edges folds to exactly {unassigned}
# ---------------------------------------------------------------------------


def test_ac4_zero_real_edges_folds_to_exactly_unassigned(repo_root):
    item_id = _make_item(repo_root)

    folded = fold_membership(repo_root=repo_root)

    assert folded[item_id] == {RESERVED_PROJECT_ID}


# ---------------------------------------------------------------------------
# AC5 — real edges fold exactly, never alongside unassigned; last-retract
# re-folds to {unassigned} with no extra write
# ---------------------------------------------------------------------------


def test_ac5_one_or_more_real_edges_exclude_unassigned(repo_root):
    item_id = _make_item(repo_root)
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    emit_project_created("proj-beta", name="Beta", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-beta", repo_root=repo_root)

    folded = fold_membership(repo_root=repo_root)

    # Review: code-reviewer (Finding 2) — the mutual-exclusivity assert this
    # test used to carry here is vacuous post-DEC-13/DEC-21 (removed; see
    # module negative-spec) and is not re-added.
    assert folded[item_id] == {"proj-alpha", "proj-beta"}


def test_ac5_retracting_a_non_last_edge_leaves_the_remainder(repo_root):
    item_id = _make_item(repo_root)
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    emit_project_created("proj-beta", name="Beta", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-beta", repo_root=repo_root)
    emit_item_project_retracted(item_id, "proj-alpha", repo_root=repo_root)

    folded = fold_membership(repo_root=repo_root)

    assert folded[item_id] == {"proj-beta"}
    assert RESERVED_PROJECT_ID not in folded[item_id]


def test_ac5_retracting_the_last_real_edge_re_folds_to_unassigned(repo_root):
    item_id = _make_item(repo_root)
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)

    events_before_retract = len(tracker_store.read_events(repo_root=repo_root))

    emit_item_project_retracted(item_id, "proj-alpha", repo_root=repo_root)

    folded = fold_membership(repo_root=repo_root)
    assert folded[item_id] == {RESERVED_PROJECT_ID}

    # No write beyond the retract event itself: exactly one more event than
    # before the retract call, and it is never a stored `unassigned` edge.
    events_after_retract = tracker_store.read_events(repo_root=repo_root)
    assert len(events_after_retract) == events_before_retract + 1
    for event in events_after_retract:
        assert event.get("project_id") != RESERVED_PROJECT_ID


# ---------------------------------------------------------------------------
# AC6 — two concurrent cross-process membership mutations on one item
# ---------------------------------------------------------------------------

_CONCURRENT_MEMBERSHIP_SCRIPT = textwrap.dedent("""\
    \"\"\"Sleep-widened concurrent membership mutator: mirrors
    test_tracker_store.py's AC1 technique — inject an artificially-widened
    lock-hold window on tracker_store.locked_rmw so two processes racing on
    the same item's membership are highly likely to actually contend rather
    than happen to interleave without ever overlapping.
    \"\"\"
    import sys, time
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    from coordinator_core import tracker_store as ts
    from coordinator_core import tracker_entities as te

    repo_root = Path(sys.argv[2])
    item_id = sys.argv[3]
    project_id = sys.argv[4]
    action = sys.argv[5]
    delay = float(sys.argv[6])

    _orig_locked_rmw = ts.locked_rmw

    def _slow_locked_rmw(target, mutate, **kwargs):
        def _slow_mutate(old_text):
            time.sleep(delay)
            return mutate(old_text)
        return _orig_locked_rmw(target, _slow_mutate, **kwargs)

    ts.locked_rmw = _slow_locked_rmw

    if action == "add":
        te.emit_item_project_added(item_id, project_id, repo_root=repo_root)
    else:
        te.emit_item_project_retracted(item_id, project_id, repo_root=repo_root)
""")


def test_ac6_two_concurrent_processes_no_lost_update_no_interleaved_artifact(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    item_id = _make_item(repo)
    emit_project_created("proj-a", name="A", repo_root=repo)
    emit_project_created("proj-b", name="B", repo_root=repo)
    events_before = len(tracker_store.read_events(repo_root=repo))

    script = tmp_path / "concurrent_membership.py"
    script.write_text(_CONCURRENT_MEMBERSHIP_SCRIPT, encoding="utf-8")

    p1 = subprocess.Popen(
        [sys.executable, str(script), _PROJECT_ROOT, str(repo), item_id, "proj-a", "add", "0.05"],
        creationflags=_NO_WINDOW,
    )
    p2 = subprocess.Popen(
        [sys.executable, str(script), _PROJECT_ROOT, str(repo), item_id, "proj-b", "add", "0.05"],
        creationflags=_NO_WINDOW,
    )
    p1.wait(timeout=60)
    p2.wait(timeout=60)
    assert p1.returncode == 0
    assert p2.returncode == 0

    events_after = tracker_store.read_events(repo_root=repo)
    assert len(events_after) == events_before + 2, "a concurrent membership mutation was lost"

    folded = fold_membership(repo_root=repo)
    # Deterministic fold of BOTH mutations regardless of interleaving order —
    # no lost update, no interleaved artifact (e.g. only one edge landing,
    # or a third phantom edge).
    assert folded[item_id] == {"proj-a", "proj-b"}


# ---------------------------------------------------------------------------
# AC12 — no projection logic lives in tracker_store.read_events
# ---------------------------------------------------------------------------


def test_ac12_read_events_signature_is_unwidened_by_this_chunk():
    # read_events is frozen (AC10); this chunk must not have added a filter,
    # projection, or pagination parameter to make its own fold easier.
    signature = inspect.signature(tracker_store.read_events)
    assert list(signature.parameters) == ["repo_root"]
    assert signature.parameters["repo_root"].kind == inspect.Parameter.KEYWORD_ONLY


def test_ac12_read_events_source_carries_no_projection_vocabulary():
    source = inspect.getsource(tracker_store.read_events)
    for forbidden in ("project_id", "RESERVED_PROJECT_ID", "fold_membership", "membership"):
        assert forbidden not in source, (
            f"{forbidden!r} found in tracker_store.read_events — projection logic "
            "has leaked into the frozen store (DEC-12 violation)"
        )


# ---------------------------------------------------------------------------
# AC16 — the reserved unassigned project row is addressable regardless of
# whether any item currently folds to it
# ---------------------------------------------------------------------------


def test_ac16_reserved_project_id_addressable_with_no_item_folding_to_it(repo_root):
    item_id = _make_item(repo_root)
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)

    folded = fold_membership(repo_root=repo_root)
    # No item currently folds to unassigned in this fixture...
    assert all(RESERVED_PROJECT_ID not in projects for projects in folded.values())
    # Review: code-reviewer (Finding 3) — `assert RESERVED_PROJECT_ID ==
    # "unassigned"` was a constant compared to its own literal (tautology,
    # cannot fail) and did not probe AC16's actual claim. On re-check: a
    # `project_created` event for RESERVED_PROJECT_ID can never exist to be
    # looked up via `tracker_store.read_events` — creating one is REJECTED
    # at construction time (see `test_tracker_entities.py`'s AC2 coverage,
    # `reject_reserved_project`). The reserved row's "addressability" is by
    # construction, not storage: it's a stable identity constant this
    # module's fold emits directly, never something read back from the
    # event stream. The assert above is this module's actual AC16-relevant
    # coverage: the fold never confuses RESERVED_PROJECT_ID with a real,
    # stored edge, regardless of fold state.


def test_ac16_reserved_project_id_addressable_when_an_item_does_fold_to_it(repo_root):
    item_id = _make_item(repo_root)

    folded = fold_membership(repo_root=repo_root)
    # Review: code-reviewer (Finding 3) — dropped the tautological
    # `assert RESERVED_PROJECT_ID == "unassigned"`. Per the sibling test
    # above: a project_created event for RESERVED_PROJECT_ID can never
    # exist (rejected at construction — test_tracker_entities.py AC2), so
    # "addressability" here is by construction (a stable identity constant
    # this fold emits directly), not a storage lookup this module could
    # test. This test's actual AC16-relevant coverage: an item with zero
    # real edges folds to exactly {RESERVED_PROJECT_ID}, established below.
    assert folded[item_id] == {RESERVED_PROJECT_ID}


# ---------------------------------------------------------------------------
# AC18 — the wire-shape helper: materialized projects[] with "unassigned"
# present, never an empty array, never raw events
# ---------------------------------------------------------------------------


def test_ac18_wire_helper_emits_unassigned_for_zero_real_edge_item(repo_root):
    item_id = _make_item(repo_root)

    wire = fold_membership_wire(repo_root=repo_root)

    assert wire[item_id] == [RESERVED_PROJECT_ID]
    assert wire[item_id] != []


def test_ac18_wire_helper_emits_sorted_real_edges_never_unassigned(repo_root):
    item_id = _make_item(repo_root)
    emit_project_created("proj-zeta", name="Zeta", repo_root=repo_root)
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-zeta", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)

    wire = fold_membership_wire(repo_root=repo_root)

    assert wire[item_id] == ["proj-alpha", "proj-zeta"]
    assert RESERVED_PROJECT_ID not in wire[item_id]


def test_ac18_wire_helper_never_returns_raw_event_shape(repo_root):
    item_id = _make_item(repo_root)
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)

    wire = fold_membership_wire(repo_root=repo_root)

    assert isinstance(wire[item_id], list)
    assert all(isinstance(entry, str) for entry in wire[item_id])
    # No event-shaped keys (kind/item_id/applied_at/...) ever appear in the
    # wire value — it is a plain list of project-id strings only.
    assert not any(isinstance(entry, dict) for entry in wire[item_id])


# ---------------------------------------------------------------------------
# AC9 — item_person: (item, person, assignee) and (item, person,
# raised_by) admitted simultaneously; an exact duplicate triple is rejected
# ---------------------------------------------------------------------------


def test_ac9_two_distinct_roles_for_same_item_person_admitted_simultaneously(repo_root):
    item_id = _make_item(repo_root)

    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo_root)
    emit_item_person_added(item_id, "person-1", "raised_by", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)

    assert folded[item_id] == {("person-1", "assignee"), ("person-1", "raised_by")}


def test_ac9_exact_duplicate_triple_is_rejected(repo_root):
    item_id = _make_item(repo_root)
    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo_root)

    with pytest.raises(TrackerEntityError):
        emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)
    assert folded[item_id] == {("person-1", "assignee")}


def test_ac9_retract_then_readd_of_same_triple_is_not_a_duplicate(repo_root):
    item_id = _make_item(repo_root)
    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo_root)
    emit_item_person_retracted(item_id, "person-1", "assignee", repo_root=repo_root)

    # The triple is currently absent (retracted), so re-adding it is not a
    # duplicate — DEC-18's key rejects a CURRENTLY-present duplicate, not a
    # historical one.
    emit_item_person_added(item_id, "person-1", "assignee", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)
    assert folded[item_id] == {("person-1", "assignee")}


def test_ac9_different_persons_same_role_are_not_duplicates(repo_root):
    item_id = _make_item(repo_root)
    emit_item_person_added(item_id, "person-1", "watcher", repo_root=repo_root)
    emit_item_person_added(item_id, "person-2", "watcher", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)
    assert folded[item_id] == {("person-1", "watcher"), ("person-2", "watcher")}


def test_ac9_invalid_role_rejected_by_both_add_and_retract(repo_root):
    item_id = _make_item(repo_root)

    with pytest.raises(TrackerEntityError):
        emit_item_person_added(item_id, "person-1", "bogus-role", repo_root=repo_root)

    with pytest.raises(TrackerEntityError):
        emit_item_person_retracted(item_id, "person-1", "bogus-role", repo_root=repo_root)


def test_ac9_null_person_id_is_tolerated(repo_root):
    item_id = _make_item(repo_root)

    emit_item_person_added(item_id, None, "mentioned", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)
    assert folded[item_id] == {(None, "mentioned")}
