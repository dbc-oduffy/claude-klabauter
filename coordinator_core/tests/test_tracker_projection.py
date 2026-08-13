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
import random
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import json
import threading

from coordinator_core import tracker_entities, tracker_store
from coordinator_core import tracker_transitions as tt
from coordinator_core.tracker_entities import (
    RESERVED_PROJECT_ID,
    TrackerEntityError,
    emit_item_created,
    emit_item_person_added,
    emit_item_person_retracted,
    emit_item_project_added,
    emit_item_project_retracted,
    emit_person_alias_added,
    emit_person_alias_retracted,
    emit_person_created,
    emit_person_merged,
    emit_project_created,
    item_project_added,
    item_project_retracted,
    mint_item_id,
    mint_person_id,
)
from coordinator_core.tracker_projection import (
    current_state,
    fold_membership,
    fold_membership_wire,
    fold_person_membership,
    fold_person_registry,
    render_status,
    resolve_alias,
    resolve_person,
)
from coordinator_core.tracker_store import shard_path

_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Declared, not excused: `append_event`'s `locked_rmw` resolves its lock directory via
# real `git rev-parse --git-common-dir`, so a bare non-git tmp_path fails before this
# file's own fold logic runs -- a real repo is load-bearing setup, not a choice. AC6
# additionally spawns a real subprocess holder to model the ccos-4 cross-process
# lost-update shape, which no mock reproduces. Fixtures are per-test because AC5/AC6
# each build distinct event histories that would collide if shared.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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


# ---------------------------------------------------------------------------
# C5b — person registry fold, multi-hop resolution, ghost sentinel,
# item_person resolution (AC6, AC7b, AC8, AC9, AC10)
# ---------------------------------------------------------------------------


def _write_raw_event_line(shard_file, event: dict) -> None:
    """Append one hand-constructed event line DIRECTLY to *shard_file*,
    bypassing every emitter and write-time guard in `tracker_entities`.

    Used only to construct shard states no legitimate emission path can
    produce: a same-machine self-referential `person_merged` edge (the
    write-side cycle guard in `emit_person_merged` refuses it), and a
    cross-shard merge cycle assembled from two independently-written
    shards (a single machine's write-side guard can never see a peer
    shard's concurrent edge). Constructing that state directly is exactly
    the point of the tests that use this helper — see
    `resolve_person`'s own docstring on why the fold-time cycle-drop rule,
    not the write-side guard, is the real cross-shard guarantee.
    """
    shard_file.parent.mkdir(parents=True, exist_ok=True)
    with shard_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def _raw_merge_event(
    from_id: str,
    into_id: str,
    *,
    applied_at: str,
    event_id: str,
    observed_at: str | None = None,
) -> dict:
    """A hand-built `person_merged` event, for direct shard injection via
    `_write_raw_event_line` — never routed through `emit_person_merged`.
    `observed_at` defaults to `applied_at` but can be set independently to
    exercise the `(applied_at, observed_at, id)` sort key's tie-break legs."""
    return {
        "kind": "person_merged",
        "from_id": from_id,
        "into_id": into_id,
        "actor": "test-harness",
        "id": event_id,
        "applied_at": applied_at,
        "observed_at": observed_at if observed_at is not None else applied_at,
    }


def test_resolve_person_multi_hop_chain(repo_root):
    """AC6 — A merged into B, B merged into C; `resolve_person(A)` walks
    the two-hop chain and returns the surviving id C."""
    a_id, b_id, c_id = mint_person_id(), mint_person_id(), mint_person_id()
    emit_person_created(a_id, display_name="A", repo_root=repo_root)
    emit_person_created(b_id, display_name="B", repo_root=repo_root)
    emit_person_created(c_id, display_name="C", repo_root=repo_root)
    emit_person_merged(a_id, b_id, "test-actor", repo_root=repo_root)
    emit_person_merged(b_id, c_id, "test-actor", repo_root=repo_root)

    registry = fold_person_registry(repo_root=repo_root)

    assert resolve_person(a_id, registry=registry) == c_id


def test_resolve_person_raises_on_self_merge_or_malformed_chain(repo_root):
    """A genuinely unreachable malformed chain — `from_id == into_id` —
    hand-written DIRECTLY into the shard, bypassing `emit_person_merged`'s
    write-side cycle guard (which refuses this at emission time). This is
    the point: `resolve_person` must RAISE rather than hang on a state the
    write side can never itself produce. Timeout-guarded so a regression
    that turns this into an infinite loop fails the test instead of
    wedging the run.

    Uses a raw daemon thread rather than `ThreadPoolExecutor` — Review:
    coordinator:code-reviewer flagged that `ThreadPoolExecutor.__exit__`
    calls `shutdown(wait=True)` by default, so on a genuine infinite loop
    the `with` block's teardown blocks forever even though
    `future.result(timeout=5)` itself correctly times out; a non-daemon
    executor thread would also keep the whole interpreter alive at
    process exit. A daemon thread that is merely abandoned (never
    joined past the timeout) fails the test without wedging either the
    suite or the process."""
    a_id = mint_person_id()
    emit_person_created(a_id, display_name="A", repo_root=repo_root)
    _write_raw_event_line(
        shard_path(repo_root),
        _raw_merge_event(
            a_id,
            a_id,
            applied_at="2026-08-11T09:00:00.000000Z",
            event_id="evt-test-selfmerge",
        ),
    )

    registry = fold_person_registry(repo_root=repo_root)

    outcome: dict[str, object] = {}

    def _run() -> None:
        try:
            outcome["value"] = resolve_person(a_id, registry=registry)
        except BaseException as exc:  # noqa: BLE001 - hand the exception to the main thread
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), (
        "resolve_person did not return within 5s on a malformed self-merge "
        "chain -- likely wedged in an infinite walk; the worker thread is a "
        "daemon and is abandoned here rather than joined further"
    )
    assert "error" in outcome, (
        f"expected TrackerEntityError, resolve_person returned {outcome.get('value')!r}"
    )
    assert isinstance(outcome["error"], TrackerEntityError)


def test_cross_shard_merge_cycle_resolves_deterministically_no_raise(repo_root, monkeypatch):
    """AC7b, DEC-42, DEC-49 — a merge cycle assembled ACROSS two shards
    (machine A writes A->B, machine B independently writes B->A), which
    `emit_person_merged`'s same-machine write-side guard can never see.
    Written directly into two shard files, never through the emitters.

    Asserts the fold-time cycle-drop rule is deterministic: both machines'
    shards drop the SAME cycle-closing edge (the one sorting last under
    `read_events`' own `(applied_at, observed_at, id)` order), and both
    directions resolve to the same surviving id with no raise — the
    property that lets two machines converge with no cross-shard
    coordination.

    Exercises all three legs of the tuple's tie-break order, each in its
    own independent cycle pair sharing the SAME `repo_root` and machine
    shards: `applied_at` differs (primary key), `applied_at` ties and
    `observed_at` differs (first tie-break), and both `applied_at` and
    `observed_at` tie so `id` alone decides (second tie-break) — Review:
    coordinator:code-reviewer flagged the original version as exercising
    only the primary key despite the docstring's full-tuple claim.
    """
    a_id, b_id = mint_person_id(), mint_person_id()
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-a")
    emit_person_created(a_id, display_name="A", repo_root=repo_root)
    emit_person_created(b_id, display_name="B", repo_root=repo_root)

    earlier_edge = _raw_merge_event(
        a_id, b_id, applied_at="2026-08-11T10:00:00.000000Z", event_id="evt-machine-a-edge"
    )
    later_edge = _raw_merge_event(
        b_id, a_id, applied_at="2026-08-11T10:00:01.000000Z", event_id="evt-machine-b-edge"
    )
    _write_raw_event_line(shard_path(repo_root, machine="machine-a"), earlier_edge)
    _write_raw_event_line(shard_path(repo_root, machine="machine-b"), later_edge)

    registry = fold_person_registry(repo_root=repo_root)

    # The later-sorting edge (machine-b's B->A) is the one dropped; the
    # walk from either direction therefore stops at its source node, B.
    result_from_a = resolve_person(a_id, registry=registry)
    result_from_b = resolve_person(b_id, registry=registry)
    assert result_from_a == b_id
    assert result_from_b == b_id
    assert result_from_a == result_from_b, "cross-shard cycle resolution is not deterministic"

    # Second pair: applied_at TIES, observed_at differs — the first
    # tie-break leg alone must decide which edge is dropped.
    c_id, d_id = mint_person_id(), mint_person_id()
    emit_person_created(c_id, display_name="C", repo_root=repo_root)
    emit_person_created(d_id, display_name="D", repo_root=repo_root)

    tied_applied_at = "2026-08-11T11:00:00.000000Z"
    earlier_observed_edge = _raw_merge_event(
        c_id,
        d_id,
        applied_at=tied_applied_at,
        observed_at="2026-08-11T11:00:00.000000Z",
        event_id="evt-machine-a-observed-edge",
    )
    later_observed_edge = _raw_merge_event(
        d_id,
        c_id,
        applied_at=tied_applied_at,
        observed_at="2026-08-11T11:00:01.000000Z",
        event_id="evt-machine-b-observed-edge",
    )
    _write_raw_event_line(shard_path(repo_root, machine="machine-a"), earlier_observed_edge)
    _write_raw_event_line(shard_path(repo_root, machine="machine-b"), later_observed_edge)

    registry = fold_person_registry(repo_root=repo_root)

    # The later-observed edge (machine-b's D->C) is dropped; both walks
    # stop at its source node, D.
    result_from_c = resolve_person(c_id, registry=registry)
    result_from_d = resolve_person(d_id, registry=registry)
    assert result_from_c == d_id
    assert result_from_d == d_id
    assert result_from_c == result_from_d, "observed_at tie-break is not deterministic"

    # Third pair: applied_at AND observed_at both TIE — `id` alone must
    # decide which edge is dropped (the lexicographically-later id).
    e_id, f_id = mint_person_id(), mint_person_id()
    emit_person_created(e_id, display_name="E", repo_root=repo_root)
    emit_person_created(f_id, display_name="F", repo_root=repo_root)

    tied_applied_observed_at = "2026-08-11T12:00:00.000000Z"
    lower_id_edge = _raw_merge_event(
        e_id,
        f_id,
        applied_at=tied_applied_observed_at,
        observed_at=tied_applied_observed_at,
        event_id="evt-id-tiebreak-0-lower",
    )
    higher_id_edge = _raw_merge_event(
        f_id,
        e_id,
        applied_at=tied_applied_observed_at,
        observed_at=tied_applied_observed_at,
        event_id="evt-id-tiebreak-1-higher",
    )
    _write_raw_event_line(shard_path(repo_root, machine="machine-a"), lower_id_edge)
    _write_raw_event_line(shard_path(repo_root, machine="machine-b"), higher_id_edge)

    registry = fold_person_registry(repo_root=repo_root)

    # The higher-id edge (F->E) is dropped; both walks stop at its
    # source node, F.
    result_from_e = resolve_person(e_id, registry=registry)
    result_from_f = resolve_person(f_id, registry=registry)
    assert result_from_e == f_id
    assert result_from_f == f_id
    assert result_from_e == result_from_f, "id tie-break is not deterministic"


def test_ghost_sentinel_zero_git_aliases(repo_root):
    """AC8, DEC-41 — a person with `email` + `display` aliases only (the
    CEO/designer case: someone who never commits) still resolves
    `item_person` attribution correctly, with the `git_author` namespace
    NEVER populated for them."""
    person_id = mint_person_id()
    emit_person_created(person_id, display_name="Designer", repo_root=repo_root)
    emit_person_alias_added(person_id, "email", "designer@example.test", repo_root=repo_root)
    emit_person_alias_added(person_id, "display", "The Designer", repo_root=repo_root)

    item_id = _make_item(repo_root)
    emit_item_person_added(item_id, person_id, "assignee", repo_root=repo_root)

    registry = fold_person_registry(repo_root=repo_root)
    folded = fold_person_membership(repo_root=repo_root)

    assert (person_id, "assignee") in folded[item_id]
    # Review: coordinator:code-reviewer — scope to this person, not the
    # whole registry, so an unrelated person's git_author alias could not
    # mask a regression on THIS person acquiring one.
    assert all(
        namespace != "git_author"
        for (namespace, _value), mapped_person_id in registry["aliases"].items()
        if mapped_person_id == person_id
    ), "a ghost-sentinel person must never have a git_author alias populated"


def test_fold_person_membership_resolves_retired_id(repo_root):
    """AC9 — an item_person edge recorded against a since-merged (retired)
    person id resolves, at fold time, to the surviving id; the retired id
    never appears in the fold's output."""
    losing_id, surviving_id = mint_person_id(), mint_person_id()
    emit_person_created(losing_id, display_name="Losing", repo_root=repo_root)
    emit_person_created(surviving_id, display_name="Surviving", repo_root=repo_root)

    item_id = _make_item(repo_root)
    emit_item_person_added(item_id, losing_id, "assignee", repo_root=repo_root)

    emit_person_merged(losing_id, surviving_id, "test-actor", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)

    assert (surviving_id, "assignee") in folded[item_id]
    assert losing_id not in {person_id for person_id, _role in folded[item_id]}


def test_fold_person_membership_null_person_id_unchanged(repo_root):
    """AC10, DEC-48 regression guard — an item_person edge carrying
    `person_id: None` still folds unchanged. Resolution through the
    person registry is additive, never a tightening: a null person_id
    never gets routed through `resolve_person`."""
    item_id = _make_item(repo_root)
    emit_item_person_added(item_id, None, "mentioned", repo_root=repo_root)

    folded = fold_person_membership(repo_root=repo_root)

    assert folded[item_id] == {(None, "mentioned")}


def test_alias_retract_then_readd_resolves(repo_root):
    """Latest-wins over the alias map: an alias retract makes
    `resolve_alias` return None; a subsequent re-add of the same
    normalized value resolves again, to the same person."""
    person_id = mint_person_id()
    emit_person_created(person_id, display_name="Person", repo_root=repo_root)
    emit_person_alias_added(person_id, "email", "person@example.test", repo_root=repo_root)

    registry = fold_person_registry(repo_root=repo_root)
    assert resolve_alias("email", "person@example.test", registry=registry) == person_id

    emit_person_alias_retracted(person_id, "email", "person@example.test", repo_root=repo_root)
    registry = fold_person_registry(repo_root=repo_root)
    assert resolve_alias("email", "person@example.test", registry=registry) is None

    emit_person_alias_added(person_id, "email", "PERSON@example.test", repo_root=repo_root)
    registry = fold_person_registry(repo_root=repo_root)
    assert resolve_alias("email", "person@example.test", registry=registry) == person_id


# ---------------------------------------------------------------------------
# C9c — chunk C9's projection-suite third: `current_state`/`render_status`
# truth table (AC6), a `manual_close` null-`from_state` reopen (AC7), the
# `(applied_at, observed_at, id)` tie-break (AC8), compaction round-trip
# (AC10), and append-only compaction (AC12).
#
# Spec backlink: docs/plans/2026-08-11-sat-03-event-sourced-completion-core.md
# § Tasks C9.
# ---------------------------------------------------------------------------

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _iso(dt: datetime) -> str:
    # Full microsecond precision -- production's own `_stamp_applied_at()`
    # (`tracker_transitions.py`) stamps via `datetime.now(timezone.utc).
    # isoformat(timespec="microseconds")`, never millisecond-truncated.
    # Zeroing the last 3 digits here (the pre-fix form) made a
    # test-fabricated `applied_at` sort ambiguously close to a real,
    # full-precision production stamp -- see AC10's "strictly between"
    # regression test, which intermittently failed on exactly this seam.
    return dt.strftime(_ISO_FMT) + "Z"


def _parse_iso(value: str) -> datetime:
    """Parse either this module's own `Z`-suffixed fabricated timestamps or
    a real `_stamp_applied_at`-minted `+00:00`-suffixed one."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _raw_transition_event(
    item_id: str,
    axis: str,
    to_state: str | None,
    *,
    event_id: str,
    applied_at: str,
    observed_at: str | None = None,
    from_state: str | None = None,
) -> dict:
    """A hand-built transition-event dict, for direct shard injection via
    `_write_raw_event_line` — never routed through `tracker_transitions`'
    emitters. Used only where the test needs exact control over
    `applied_at`/`observed_at`/`id` that a real emitter's own timestamp
    stamping cannot give (AC8's tie-break, AC10's offline-merge case)."""
    return {
        "id": event_id,
        "item_id": item_id,
        "axis": axis,
        "from_state": from_state,
        "to_state": to_state,
        "actor": "test-harness",
        "evidence": None,
        "tier": "direct",
        "source_observation_id": None,
        "applied_at": applied_at,
        "observed_at": observed_at if observed_at is not None else applied_at,
        "schema_version": 1,
    }


# ---------------------------------------------------------------------------
# AC6 — truth table over all three axes
# ---------------------------------------------------------------------------


def test_ac6_no_events_at_all_reads_open(repo_root):
    item_id = _make_item(repo_root)
    assert render_status(item_id, repo_root=repo_root) == "open"


def test_ac6_manual_close_closed_alone_closes(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id, "manual_close", "closed", actor="a", tier="direct", repo_root=repo_root
    )
    assert render_status(item_id, repo_root=repo_root) == "closed"


def test_ac6_code_complete_asserted_and_qa_verified_verified_closes(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id, "code_complete", "asserted", actor="a", tier="direct", repo_root=repo_root
    )
    tt.emit_transition(
        item_id, "qa_verified", "verified", actor="a", tier="direct", repo_root=repo_root
    )
    assert render_status(item_id, repo_root=repo_root) == "closed"


def test_ac6_asserted_but_not_verified_stays_open(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id, "code_complete", "asserted", actor="a", tier="direct", repo_root=repo_root
    )
    assert render_status(item_id, repo_root=repo_root) == "open"


def test_ac6_verified_but_not_asserted_stays_open(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id, "qa_verified", "verified", actor="a", tier="direct", repo_root=repo_root
    )
    assert render_status(item_id, repo_root=repo_root) == "open"


def test_ac6_manual_close_closes_even_when_other_two_axes_are_incomplete(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id, "manual_close", "closed", actor="a", tier="direct", repo_root=repo_root
    )
    tt.emit_transition(
        item_id, "code_complete", "asserted", actor="a", tier="direct", repo_root=repo_root
    )
    # qa_verified never asserted — the manual_close path alone must still
    # close this item.
    assert render_status(item_id, repo_root=repo_root) == "closed"


# ---------------------------------------------------------------------------
# AC7 — a manual_close reopen with from_state=null folds correctly
# ---------------------------------------------------------------------------


def test_ac7_manual_close_reopen_with_null_from_state_folds_correctly(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id,
        "manual_close",
        "reopened",
        from_state=None,
        actor="a",
        tier="direct",
        repo_root=repo_root,
    )

    assert current_state(item_id, "manual_close", repo_root=repo_root) == "reopened"
    assert render_status(item_id, repo_root=repo_root) == "open"


def test_ac7_manual_close_reopen_after_a_prior_close_reopens_the_item(repo_root):
    item_id = _make_item(repo_root)
    tt.emit_transition(
        item_id, "manual_close", "closed", actor="a", tier="direct", repo_root=repo_root
    )
    assert render_status(item_id, repo_root=repo_root) == "closed"

    tt.emit_transition(
        item_id,
        "manual_close",
        "reopened",
        from_state=None,
        actor="a",
        tier="direct",
        repo_root=repo_root,
    )

    assert current_state(item_id, "manual_close", repo_root=repo_root) == "reopened"
    assert render_status(item_id, repo_root=repo_root) == "open"


# ---------------------------------------------------------------------------
# AC8 — (applied_at, observed_at, id) tie-break, hand-constructed events
# ---------------------------------------------------------------------------


def test_ac8_identical_applied_at_resolves_by_observed_at(repo_root):
    item_id = _make_item(repo_root)
    shard = shard_path(repo_root)
    common_applied_at = "2026-08-11T10:00:00.000000Z"

    earlier_observed = _raw_transition_event(
        item_id,
        "code_complete",
        "asserted",
        event_id="evt-tie-observed-a",
        applied_at=common_applied_at,
        observed_at="2026-08-11T09:00:00.000000Z",
    )
    later_observed = _raw_transition_event(
        item_id,
        "code_complete",
        "retracted",
        event_id="evt-tie-observed-b",
        applied_at=common_applied_at,
        observed_at="2026-08-11T11:00:00.000000Z",
    )
    # Written in reverse-of-winning order, to prove the winner is
    # determined by (applied_at, observed_at) ordering, never by
    # append/write order.
    _write_raw_event_line(shard, later_observed)
    _write_raw_event_line(shard, earlier_observed)

    assert current_state(item_id, "code_complete", repo_root=repo_root) == "retracted"


def test_ac8_identical_applied_at_and_observed_at_resolves_by_id(repo_root):
    item_id = _make_item(repo_root)
    shard = shard_path(repo_root)
    common_ts = "2026-08-11T10:00:00.000000Z"

    lower_id = _raw_transition_event(
        item_id,
        "qa_verified",
        "verified",
        event_id="evt-aaa-tie-id-lower",
        applied_at=common_ts,
        observed_at=common_ts,
    )
    higher_id = _raw_transition_event(
        item_id,
        "qa_verified",
        "retracted",
        event_id="evt-zzz-tie-id-higher",
        applied_at=common_ts,
        observed_at=common_ts,
    )
    # Written in reverse-of-winning order, same rationale as above.
    _write_raw_event_line(shard, higher_id)
    _write_raw_event_line(shard, lower_id)

    assert current_state(item_id, "qa_verified", repo_root=repo_root) == "retracted"


# ---------------------------------------------------------------------------
# AC10 — compaction round-trip: randomised property (fixed seed, stdlib
# random, no hypothesis dependency) plus the offline-merge named case
# ---------------------------------------------------------------------------


def _reference_axis_state(events: list[dict], item_id: str, axis: str) -> str | None:
    """An independent, from-scratch reference fold over raw (non-snapshot)
    transition events — never calling `tracker_projection` — used as the
    "full replay" oracle the property/named tests below compare against."""
    state: str | None = None
    for event in sorted(
        events, key=lambda e: (e.get("applied_at"), e.get("observed_at"), e.get("id"))
    ):
        if event.get("kind") == "snapshot":
            continue
        if event.get("item_id") != item_id or event.get("axis") != axis:
            continue
        to_state = event.get("to_state")
        if to_state is not None:
            state = to_state
    return state


def test_ac10_compaction_round_trip_property_fixed_seed(tmp_path):
    """Property: for a randomised event sequence and a randomised
    checkpoint, projecting over [snapshot, events_after] equals projecting
    over the full replay. stdlib `random` with a fixed seed — no
    `hypothesis` dependency added."""
    rng = random.Random(20260811)
    axis = "code_complete"
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)

    for trial in range(15):
        repo = _make_git_repo(tmp_path / f"repo-ac10-property-{trial}")
        item_id = _make_item(repo, title=f"Trial{trial}", body="ac10 property trial")

        n_events = rng.randint(4, 24)
        events = []
        for i in range(n_events):
            applied_at = _iso(base + timedelta(seconds=i, microseconds=trial))
            to_state = rng.choice(["asserted", "retracted"])
            events.append(
                _raw_transition_event(
                    item_id,
                    axis,
                    to_state,
                    event_id=f"evt-ac10-prop-{trial}-{i:03d}",
                    applied_at=applied_at,
                )
            )
        for event in events:
            _write_raw_event_line(shard_path(repo), event)

        checkpoint = rng.randint(1, n_events)
        folded = events[:checkpoint]
        events_after = events[checkpoint:]

        folded_to_state = None
        for event in folded:
            if event["to_state"] is not None:
                folded_to_state = event["to_state"]

        snapshot_payload = tt.build_snapshot_event(
            item_id,
            axis,
            folded_event_ids=[event["id"] for event in folded],
            as_of_sequence=0,
            as_of_applied_at=folded[-1]["applied_at"],
            folded_to_state=folded_to_state,
        )
        # Stamped by hand so the snapshot line sorts strictly between the
        # folded window and events_after — mirroring the real fold
        # boundary. `emit_snapshot_event`'s wall-clock "now" stamp would
        # sort AFTER every fabricated 2020-dated event and swallow
        # events_after out of the merged order entirely.
        snapshot_applied_at = _iso(
            _parse_iso(folded[-1]["applied_at"]) + timedelta(microseconds=1)
        )
        snapshot_event = dict(snapshot_payload)
        snapshot_event["observed_at"] = snapshot_applied_at
        snapshot_event["applied_at"] = snapshot_applied_at
        snapshot_event["folded_at"] = snapshot_applied_at
        snapshot_event["schema_version"] = 1
        snapshot_event["id"] = f"evt-ac10-prop-{trial}-snapshot"
        _write_raw_event_line(shard_path(repo), snapshot_event)

        expected = _reference_axis_state(events, item_id, axis)
        actual = current_state(item_id, axis, repo_root=repo)
        assert actual == expected, (
            f"trial {trial}: checkpoint={checkpoint}/{n_events} diverged from full replay"
        )


def test_ac10_content_bound_skip_survives_late_earlier_event_across_shards(
    repo_root, monkeypatch
):
    """Named case (the one that matters — not left to the happy-path
    property above): fold a snapshot, then inject an event whose
    `applied_at` is STRICTLY EARLIER than the snapshot's `as_of_applied_at`
    into a SECOND shard, and assert the projection still equals full
    replay. Proves `folded_event_ids`' skip is content-bound (by exact
    event id), never position-bound — a position-bound cursor would drop
    this late-arriving offline event and turn this test red."""
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-a")
    item_id = _make_item(repo_root)
    axis = "code_complete"

    tt.emit_transition(
        item_id, axis, "asserted", actor="a", tier="direct", repo_root=repo_root
    )
    tt.emit_transition(
        item_id, axis, "retracted", actor="a", tier="direct", repo_root=repo_root
    )

    snapshot = tt.snapshot_axis_if_due(item_id, axis, repo_root=repo_root)
    assert snapshot is not None
    assert snapshot["kind"] == "snapshot"
    as_of_applied_at = snapshot["as_of_applied_at"]

    # Strictly earlier than the snapshot's own as_of_applied_at, minted on
    # a machine ("machine-b") that never saw the fold — the exact
    # offline-merge shape this skip mechanism exists to survive.
    earlier_dt = _parse_iso(as_of_applied_at) - timedelta(hours=1)
    late_event = _raw_transition_event(
        item_id,
        axis,
        "asserted",
        event_id="evt-machine-b-late-offline",
        applied_at=_iso(earlier_dt),
    )
    assert late_event["applied_at"] < as_of_applied_at

    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-b")
    _write_raw_event_line(shard_path(repo_root, machine="machine-b"), late_event)

    all_events = tracker_store.read_events(repo_root=repo_root)
    real_events = [event for event in all_events if event.get("kind") != "snapshot"]
    expected = _reference_axis_state(real_events, item_id, axis)

    actual = current_state(item_id, axis, repo_root=repo_root)
    assert actual == expected, (
        "content-bound folded_event_ids skip diverged from full replay under "
        "an offline-merge late-earlier event — see AC10 named-case docstring"
    )


# ---------------------------------------------------------------------------
# AC12 — compaction appends, never rewrites
# ---------------------------------------------------------------------------


def test_ac10_late_event_strictly_between_fold_boundary_and_snapshot_stamp(
    repo_root, monkeypatch
):
    """Regression for the P1 window neither existing AC10 test exercises:
    an un-folded event whose `applied_at` sorts STRICTLY BETWEEN the
    snapshot's `as_of_applied_at` (the fold boundary) and the snapshot
    record's own (later, compaction-time-stamped) `applied_at`.

    `read_events`' global sort places that event BEFORE the snapshot
    record — it is correctly unskipped and applied by a naive raw-order
    fold — but a fold that then hits the snapshot at its own (later) sort
    position and unconditionally overwrites state silently discards it.
    Fails pre-fix: pre-fix code processes the gap event first (state ->
    "asserted"), then reaches the snapshot and overwrites unconditionally
    (state -> "retracted"), landing on "retracted" while full replay of
    {t1 asserted, t2 retracted, gap-event asserted} is "asserted" — the
    fold and the reference oracle diverge. Post-fix, the snapshot is
    positioned at `as_of_applied_at` (before the gap event), so the gap
    event applies on top of the seed and both equal "asserted"."""
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-a")
    item_id = _make_item(repo_root)
    axis = "code_complete"

    tt.emit_transition(
        item_id, axis, "asserted", actor="a", tier="direct", repo_root=repo_root
    )
    tt.emit_transition(
        item_id, axis, "retracted", actor="a", tier="direct", repo_root=repo_root
    )

    snapshot = tt.snapshot_axis_if_due(item_id, axis, repo_root=repo_root)
    assert snapshot is not None
    assert snapshot["kind"] == "snapshot"
    as_of_applied_at = snapshot["as_of_applied_at"]
    snapshot_applied_at = snapshot["applied_at"]
    assert as_of_applied_at < snapshot_applied_at, (
        "fixture precondition: the snapshot's own applied_at (compaction "
        "wall-clock time) must sort strictly after as_of_applied_at (the "
        "fold boundary) for there to be a gap to inject an event into"
    )

    # Strictly inside (as_of_applied_at, snapshot.applied_at) — never
    # folded (postdates the fold boundary), never skipped, but sorts
    # BEFORE the snapshot record itself under read_events' raw order.
    gap_dt = (
        _parse_iso(as_of_applied_at)
        + (_parse_iso(snapshot_applied_at) - _parse_iso(as_of_applied_at)) / 2
    )
    gap_event = _raw_transition_event(
        item_id,
        axis,
        "asserted",
        event_id="evt-gap-between-boundary-and-stamp",
        applied_at=_iso(gap_dt),
    )
    assert as_of_applied_at < gap_event["applied_at"] < snapshot_applied_at

    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-b")
    _write_raw_event_line(shard_path(repo_root, machine="machine-b"), gap_event)

    all_events = tracker_store.read_events(repo_root=repo_root)
    real_events = [event for event in all_events if event.get("kind") != "snapshot"]
    expected = _reference_axis_state(real_events, item_id, axis)
    assert expected == "asserted"

    actual = current_state(item_id, axis, repo_root=repo_root)
    assert actual == expected, (
        "snapshot seed overwrote an event landing strictly between the "
        "fold boundary and the snapshot's own applied_at stamp — see P1 "
        "regression docstring"
    )


def test_ac12_compaction_appends_never_rewrites_shard_prefix(repo_root, monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-a")
    item_id = _make_item(repo_root)
    axis = "code_complete"
    tt.emit_transition(item_id, axis, "asserted", actor="a", tier="direct", repo_root=repo_root)

    shard = shard_path(repo_root, machine="machine-a")
    prefix_before = shard.read_bytes()
    assert prefix_before, "fixture setup produced an empty shard — nothing to prove append-only over"

    snapshot = tt.snapshot_axis_if_due(item_id, axis, repo_root=repo_root)
    assert snapshot is not None

    contents_after = shard.read_bytes()
    assert contents_after.startswith(prefix_before), (
        "pre-existing shard bytes were rewritten by compaction, not left untouched"
    )

    appended_bytes = contents_after[len(prefix_before) :]
    appended_text = appended_bytes.decode("utf-8")
    assert appended_text.endswith("\n")
    assert appended_text.count("\n") == 1, "compaction appended more than one new line"
    appended_event = json.loads(appended_text)
    assert appended_event["kind"] == "snapshot"
    assert appended_event["item_id"] == item_id
    assert appended_event["axis"] == axis
