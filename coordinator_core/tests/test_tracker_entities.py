"""
coordinator_core.tests.test_tracker_entities — C1/C2 entity/emission tests.

Purpose: C1 authors id-grammar/no-status/reserved-project tests explicitly
(id-grammar correctness is not safely deferred). C2 adds the emission-layer
tests co-landing with that chunk: emission round-trips through
``read_events``, the ``applied_at``-population case, AC13 (emission order
beats digest order within one wall-clock second), AC14 (add/retract/re-add
lands three distinct events), and AC17 (the DEC-24 foreign-repo refusal
raises, never silently skips).

Covers AC1 (no status key, structurally), AC2 (reserved project cannot be
created/retracted/renamed), AC7 (trailer-unsafe characters rejected; output
is [a-z0-9-] only), AC8 (same-machine identical-input mints differ;
cross-machine identical-input mints differ), AC13, AC14, AC17.

Spec backlink: docs/plans/2026-08-05-sat-02-sovereign-tracker-relational-
spine.md § Acceptance Criteria, § Tasks C1/C2.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core import tracker_entities, tracker_store
from coordinator_core.tracker_entities import (
    ALIAS_NAMESPACES,
    EVENT_KINDS,
    RESERVED_PROJECT_ID,
    TrackerEntityError,
    emit_item_created,
    emit_item_project_added,
    emit_item_project_retracted,
    emit_person_alias_added,
    emit_person_alias_retracted,
    emit_person_created,
    emit_person_merged,
    emit_project_created,
    item_created,
    item_project_added,
    mint_item_id,
    mint_person_id,
    normalize_alias,
    person_alias_added,
    person_alias_retracted,
    project_created,
    reject_invalid_namespace,
    reject_reserved_project,
)

_ITEM_ID_RE = re.compile(r"^[a-z0-9-]+$")


def test_event_kinds_closed_set_extended():
    """AC1: sat-05's C1 widened the closed EVENT_KINDS enum from six to ten
    members (the four new person kinds), and it stayed CLOSED — nothing
    else was added alongside them. Asserted against the frozenset's full
    membership, not merely `in` checks, so an accidental eleventh member
    would fail this test the same way a missing one would.

    Subsumes the pre-C1 `test_event_kinds_is_exactly_the_closed_six` (this
    is a widened rename of that test, not an additional duplicate — see
    the C5a report for why the two were folded into one)."""
    assert EVENT_KINDS == {
        "item_created",
        "project_created",
        "item_project_added",
        "item_project_retracted",
        "item_person_added",
        "item_person_retracted",
        "person_created",
        "person_alias_added",
        "person_alias_retracted",
        "person_merged",
    }


# --- AC1: item carries no status key, structurally ---


def test_item_created_carries_no_status_key():
    payload = item_created(
        "itm-20260805-widget-abc123-deadbeef0000",
        title="Widget",
        body="Do the thing",
        created_at="2026-08-05T10:00:00.000000Z",
    )
    assert "status" not in payload
    assert set(payload) == {"kind", "id", "title", "body", "created_at"}


def test_item_created_signature_admits_no_status_argument():
    with pytest.raises(TypeError):
        item_created(
            "itm-x",
            title="Widget",
            body="Do the thing",
            created_at="2026-08-05T10:00:00Z",
            status="open",  # type: ignore[call-arg]
        )


# --- AC2: reserved project cannot be created / retracted / renamed ---


def test_reserved_project_cannot_be_created():
    with pytest.raises(TrackerEntityError):
        project_created(RESERVED_PROJECT_ID, name="Unassigned")


def test_ordinary_project_creation_succeeds():
    payload = project_created("proj-alpha", name="Alpha")
    assert payload == {
        "kind": "project_created",
        "project_id": "proj-alpha",
        "name": "Alpha",
    }


def test_reject_reserved_project_guard_rejects_reserved_id_for_any_action():
    for action in ("create", "retract", "rename"):
        with pytest.raises(TrackerEntityError):
            reject_reserved_project(RESERVED_PROJECT_ID, action=action)
    # A non-reserved id is a silent no-op for every action.
    for action in ("create", "retract", "rename"):
        reject_reserved_project("proj-alpha", action=action)


def test_item_project_added_refuses_the_reserved_edge():
    with pytest.raises(TrackerEntityError):
        item_project_added("itm-x", RESERVED_PROJECT_ID)


def test_item_project_added_accepts_a_real_edge():
    payload = item_project_added("itm-x", "proj-alpha")
    assert payload == {
        "kind": "item_project_added",
        "item_id": "itm-x",
        "project_id": "proj-alpha",
    }


# --- AC7: mint_item_id output is [a-z0-9-]-only, trailer-safe ---


@pytest.mark.parametrize(
    "title,body",
    [
        ("Has\twhitespace", "body"),
        ("Has newline\nhere", "body"),
        ("Has: a colon", "body"),
        ("Title", "Body with\nnewline and: colon\tand tab"),
        ("MiXeD Case Title!!", "!!!"),
    ],
)
def test_mint_item_id_output_is_charset_clean_for_unsafe_input(title, body):
    item_id = mint_item_id(title, body, "2026-08-05T10:00:00.000000Z")
    assert _ITEM_ID_RE.match(item_id), item_id
    assert "\t" not in item_id
    assert "\n" not in item_id
    assert ":" not in item_id
    assert " " not in item_id


def test_mint_item_id_shape():
    item_id = mint_item_id("Widget", "Do the thing", "2026-08-05T10:00:00.000000Z")
    parts = item_id.split("-")
    assert parts[0] == "itm"
    assert parts[1] == "20260805"
    # nonce (6 hex chars) and digest (12 hex chars) are the last two dash
    # segments; the slug occupies everything in between.
    assert re.match(r"^[0-9a-f]{6}$", parts[-2])
    assert re.match(r"^[0-9a-f]{12}$", parts[-1])


def test_mint_item_id_slug_truncated_to_32_chars():
    long_title = "a" * 100
    item_id = mint_item_id(long_title, "body", "2026-08-05T10:00:00Z")
    parts = item_id.split("-")
    slug_segment = "-".join(parts[2:-2])
    assert len(slug_segment) <= 32


def test_mint_item_id_slug_truncation_does_not_produce_a_doubled_hyphen():
    # Review: code-reviewer c2a5a195 Finding 1 — a title whose
    # _slug_from_title output places a '-' at/near index 32 was truncated a
    # SECOND time (to 32) without re-stripping, silently emitting a doubled
    # hyphen that the [a-z0-9-] charset check never caught.
    title = "a" * 31 + " " + "b" * 20
    item_id = mint_item_id(title, "body", "2026-08-05T10:00:00Z")
    assert "--" not in item_id
    parts = item_id.split("-")
    slug_segment = "-".join(parts[2:-2])
    assert not slug_segment.startswith("-")
    assert not slug_segment.endswith("-")


def test_mint_item_id_raises_on_empty_slug():
    # A title that slugs to empty (all punctuation) must not silently mint
    # a doubled-hyphen id with an empty slug segment.
    with pytest.raises(TrackerEntityError):
        mint_item_id("!!!", "body", "2026-08-05T10:00:00Z")


def test_mint_item_id_raises_on_malformed_created_at():
    with pytest.raises(TrackerEntityError):
        mint_item_id("Widget", "body", "20260805T100000Z")


def test_mint_item_id_raises_on_malformed_explicit_nonce():
    with pytest.raises(TrackerEntityError):
        mint_item_id("Widget", "body", "2026-08-05T10:00:00Z", nonce="z")
    with pytest.raises(TrackerEntityError):
        mint_item_id("Widget", "body", "2026-08-05T10:00:00Z", nonce="deadbeefcafe")


# --- AC8: nonce (not machine-qualification) makes identical-input mints differ ---


def test_same_machine_identical_input_mints_differ():
    title, body, created_at = "Widget", "Do the thing", "2026-08-05T10:00:00.000000Z"
    first = mint_item_id(title, body, created_at)
    second = mint_item_id(title, body, created_at)
    assert first != second


def test_cross_machine_identical_input_mints_differ():
    # mint_item_id carries no machine slug at all (DEC-16) — cross-machine
    # differentiation, like same-machine differentiation, comes solely from
    # the independently-minted nonce, exercised here with two explicit
    # nonces standing in for two distinct hosts' independent mints.
    title, body, created_at = "Widget", "Do the thing", "2026-08-05T10:00:00.000000Z"
    machine_a = mint_item_id(title, body, created_at, nonce="aaaaaa")
    machine_b = mint_item_id(title, body, created_at, nonce="bbbbbb")
    assert machine_a != machine_b


def test_explicit_identical_nonce_reproduces_identical_id():
    # Sanity check on the digest formula itself: holding every input
    # (including nonce) fixed must be deterministic.
    title, body, created_at = "Widget", "Do the thing", "2026-08-05T10:00:00.000000Z"
    first = mint_item_id(title, body, created_at, nonce="cafe00")
    second = mint_item_id(title, body, created_at, nonce="cafe00")
    assert first == second


# --- C2: the emission layer ---

_EVENT_ID_RE = re.compile(r"^evt-[a-z0-9-]+-[0-9a-f]{12}$")


def _make_git_repo(root):
    """Init a minimal git repository under *root* (append_event's
    ``locked_rmw`` resolves its lock directory via ``git rev-parse
    --git-common-dir``, so a bare non-git ``tmp_path`` fails there before
    any of this chunk's own logic runs).

    Mirrors ``test_tracker_store.py``'s ``_make_git_repo`` exactly — see
    that module for the full rationale on each git-config line.
    """
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-entities-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Entities Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


@pytest.fixture
def repo_root(tmp_path):
    return _make_git_repo(tmp_path / "repo")


def _make_item(repo_root, *, title="Widget", body="Do the thing"):
    item_id = mint_item_id(title, body, "2026-08-05T10:00:00.000000Z")
    stored = emit_item_created(
        item_id,
        title=title,
        body=body,
        created_at="2026-08-05T10:00:00.000000Z",
        repo_root=repo_root,
    )
    return item_id, stored


def test_emit_item_created_round_trips_through_read_events(repo_root):
    item_id, stored = _make_item(repo_root)

    assert _EVENT_ID_RE.match(stored["id"]), stored["id"]
    assert stored["item_id"] == item_id
    assert stored["kind"] == "item_created"
    assert stored["applied_at"]
    assert stored["observed_at"]

    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1
    assert events[0]["id"] == stored["id"]
    assert events[0]["item_id"] == item_id


def test_emit_project_created_round_trips_through_read_events(repo_root):
    stored = emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    assert _EVENT_ID_RE.match(stored["id"]), stored["id"]
    assert stored["project_id"] == "proj-alpha"

    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1
    assert events[0]["kind"] == "project_created"


def test_emit_item_project_added_round_trips_and_refuses_reserved_edge(repo_root):
    item_id, _ = _make_item(repo_root)
    stored = emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)
    assert _EVENT_ID_RE.match(stored["id"]), stored["id"]
    assert stored["item_id"] == item_id
    assert stored["project_id"] == "proj-alpha"

    with pytest.raises(TrackerEntityError):
        emit_item_project_added(item_id, RESERVED_PROJECT_ID, repo_root=repo_root)


# --- applied_at population is load-bearing for read_events' fold filter ---


def test_every_emitted_event_populates_applied_at_so_it_survives_read_events(repo_root):
    item_id, _ = _make_item(repo_root)
    emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)

    events = tracker_store.read_events(repo_root=repo_root)
    # Two emissions (item_created, item_project_added); read_events would
    # silently drop any record whose applied_at is None/absent (its own
    # filter) — asserting the count AND every record's populated field
    # together is what catches a regression that stops stamping it.
    assert len(events) == 2
    for event in events:
        assert event.get("applied_at"), event


# --- AC13: emission order beats digest order within one wall-clock second ---


def test_ac13_emission_order_beats_digest_order_within_one_second(repo_root, monkeypatch):
    item_id, _ = _make_item(repo_root)

    # Same wall-clock second, strictly increasing microseconds — the exact
    # DEC-19 scenario (unassign-then-reassign inside one process/second).
    stamps = iter(
        [
            "2026-08-05T10:00:00.100000+00:00",
            "2026-08-05T10:00:00.200000+00:00",
        ]
    )
    monkeypatch.setattr(tracker_entities, "_stamp_applied_at", lambda: next(stamps))

    first = emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)
    second = emit_item_project_retracted(item_id, "proj-alpha", repo_root=repo_root)

    events = tracker_store.read_events(repo_root=repo_root)
    edge_events = [e for e in events if e["kind"] in ("item_project_added", "item_project_retracted")]
    assert [e["id"] for e in edge_events] == [first["id"], second["id"]]
    # Digest-order sanity: the two event ids do NOT happen to already sort
    # in emission order by lexical accident — proven by forcing distinct
    # applied_at values above and asserting read_events' sort key (which
    # puts applied_at ahead of id) is what actually produced this order.
    assert edge_events[0]["applied_at"] < edge_events[1]["applied_at"]


def test_stamp_applied_at_produces_distinct_back_to_back_stamps():
    # Review: code-reviewer c2a5a195 Finding 8 — the AC13 test above
    # monkeypatches _stamp_applied_at entirely, so it never verifies the
    # REAL function's microsecond-precision behavior, which is AC13's whole
    # premise (DEC-19).
    first = tracker_entities._stamp_applied_at()
    second = tracker_entities._stamp_applied_at()
    assert first != second


# --- AC14: add -> retract -> re-add lands THREE distinct events ---


def test_ac14_add_retract_readd_lands_three_distinct_events(repo_root):
    item_id, _ = _make_item(repo_root)

    added = emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)
    retracted = emit_item_project_retracted(item_id, "proj-alpha", repo_root=repo_root)
    re_added = emit_item_project_added(item_id, "proj-alpha", repo_root=repo_root)

    ids = {added["id"], retracted["id"], re_added["id"]}
    assert len(ids) == 3

    events = tracker_store.read_events(repo_root=repo_root)
    edge_events = [e for e in events if e["kind"] in ("item_project_added", "item_project_retracted")]
    assert len(edge_events) == 3


# --- AC17: the DEC-24 foreign-repo refusal raises, never silently skips ---


def test_ac17_item_project_added_raises_for_foreign_repo_item(repo_root):
    with pytest.raises(TrackerEntityError):
        emit_item_project_added("itm-not-created-here", "proj-alpha", repo_root=repo_root)

    # Never a silent skip: no event of any kind was written.
    assert tracker_store.read_events(repo_root=repo_root) == []


def test_ac17_item_project_retracted_raises_for_foreign_repo_item(repo_root):
    with pytest.raises(TrackerEntityError):
        emit_item_project_retracted("itm-not-created-here", "proj-alpha", repo_root=repo_root)

    assert tracker_store.read_events(repo_root=repo_root) == []


# --- sat-05 C5a: person/alias/merge emission-layer tests ---


def test_person_created_mints_immutable_uuid(repo_root):
    """AC2: `mint_person_id` mints a UUID4 string, and `emit_person_created`
    stores it verbatim as `person_id` — the id is never re-derived or
    re-minted by the emitter."""
    person_id = mint_person_id()
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        person_id,
    ), person_id

    stored = emit_person_created(person_id, display_name="Ada Lovelace", repo_root=repo_root)
    assert stored["kind"] == "person_created"
    assert stored["person_id"] == person_id
    assert stored["display_name"] == "Ada Lovelace"

    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1
    assert events[0]["person_id"] == person_id


def test_alias_collision_across_persons_refused(repo_root):
    """AC3: the same (namespace, normalized_value) pair claimed by a second,
    different person_id is refused at write time. The error message must
    name BOTH the existing owner and the rejected new claimant, so a
    reader debugging the collision does not need to re-query the store."""
    person_a = mint_person_id()
    person_b = mint_person_id()
    emit_person_created(person_a, display_name="Ada", repo_root=repo_root)
    emit_person_created(person_b, display_name="Bea", repo_root=repo_root)

    emit_person_alias_added(person_a, "email", "ada@example.com", repo_root=repo_root)

    with pytest.raises(TrackerEntityError) as excinfo:
        emit_person_alias_added(person_b, "email", "ada@example.com", repo_root=repo_root)

    message = str(excinfo.value)
    assert person_a in message
    assert person_b in message


def test_email_alias_case_and_whitespace_insensitive():
    """AC4/DEC-44: `email` (and `git_author`) are stripped AND casefolded —
    identity for those namespaces is case-insensitive, since the same
    mailbox/git identity commonly appears with varying case across
    sources."""
    assert normalize_alias("email", "  Ada@Example.COM  ") == "ada@example.com"
    assert normalize_alias("git_author", " Ada@Example.COM ") == "ada@example.com"

    payload = person_alias_added("person-x", "email", "  Ada@Example.COM  ")
    assert payload["raw_value"] == "  Ada@Example.COM  "
    assert payload["normalized_value"] == "ada@example.com"


def test_display_alias_case_sensitive():
    """AC4/DEC-44 asymmetry: `display` (and `transcript_name`) are stripped
    ONLY, never casefolded. This is deliberate, not an oversight — a
    human-facing display form's case is part of its identity (e.g. "Ada"
    vs "ADA" are visually and semantically distinct as DISPLAYED text, in
    a way an email mailbox's case never is), so normalize_alias must not
    "fix" this into case-insensitivity for these two namespaces."""
    assert normalize_alias("display", "  Ada Lovelace  ") == "Ada Lovelace"
    assert normalize_alias("transcript_name", "  ADA LOVELACE  ") == "ADA LOVELACE"
    assert normalize_alias("display", "ada") != normalize_alias("display", "ADA")


def test_invalid_namespace_refused_by_add_and_retract():
    """Both `person_alias_added` and `person_alias_retracted` route through
    the shared `reject_invalid_namespace` guard, mirroring
    `reject_invalid_role`'s existing shape for item_person."""
    assert "bogus" not in ALIAS_NAMESPACES

    with pytest.raises(TrackerEntityError):
        person_alias_added("person-x", "bogus", "value")
    with pytest.raises(TrackerEntityError):
        person_alias_retracted("person-x", "bogus", "value")
    with pytest.raises(TrackerEntityError):
        reject_invalid_namespace("bogus", action="add")


def test_merge_idempotent_replay_refused(repo_root):
    """AC5/DEC-43: replaying the identical `person_merged(from, into,
    actor)` call a second time is refused — the store ends up with exactly
    ONE tombstone naming `from_id`, not two."""
    person_a = mint_person_id()
    person_b = mint_person_id()
    emit_person_created(person_a, display_name="Ada", repo_root=repo_root)
    emit_person_created(person_b, display_name="Ada (dup)", repo_root=repo_root)

    emit_person_merged(person_b, person_a, "test-actor", repo_root=repo_root)

    with pytest.raises(TrackerEntityError):
        emit_person_merged(person_b, person_a, "test-actor", repo_root=repo_root)

    events = tracker_store.read_events(repo_root=repo_root)
    merge_events = [e for e in events if e["kind"] == "person_merged"]
    assert len(merge_events) == 1
    assert merge_events[0]["from_id"] == person_b
    assert merge_events[0]["into_id"] == person_a


def test_merge_cycle_rejected_and_nothing_appended(repo_root):
    """AC7/DEC-42: merging `into_id` back onto a person that already
    resolves, through the existing merge chain, to `from_id` would close a
    cycle and is refused. The assertion is on the EVENT COUNT staying
    unchanged across the raise, not merely that it raised — a guard that
    appends and THEN raises is the exact defect this test exists to
    catch."""
    person_a = mint_person_id()
    person_b = mint_person_id()
    person_c = mint_person_id()
    emit_person_created(person_a, display_name="A", repo_root=repo_root)
    emit_person_created(person_b, display_name="B", repo_root=repo_root)
    emit_person_created(person_c, display_name="C", repo_root=repo_root)

    # a -> b -> c
    emit_person_merged(person_a, person_b, "test-actor", repo_root=repo_root)
    emit_person_merged(person_b, person_c, "test-actor", repo_root=repo_root)

    before = tracker_store.read_events(repo_root=repo_root)
    before_count = len(before)

    # c -> a would close the cycle a -> b -> c -> a.
    with pytest.raises(TrackerEntityError):
        emit_person_merged(person_c, person_a, "test-actor", repo_root=repo_root)

    after = tracker_store.read_events(repo_root=repo_root)
    assert len(after) == before_count


def test_no_path_literal_in_module():
    """AC12: no `state/` or `sovereign-tracker` PATH literal appears in
    `tracker_entities.py`. This is narrower than a bare substring grep for
    "sovereign-tracker" — the shipped module legitimately says
    "sovereign-tracker" in prose (its module docstring and a `kind`-field
    docstring, describing the subsystem in English), and prose in a
    docstring is not a path literal. A future reader must not "tighten"
    this back into a bare-token grep that would false-positive on that
    prose — the assertion below is scoped to the PATH forms `state/` and
    `state/sovereign-tracker`, not the bare word."""
    source = Path(tracker_entities.__file__).read_text(encoding="utf-8")
    assert "state/sovereign-tracker" not in source
    assert "state/" not in source
