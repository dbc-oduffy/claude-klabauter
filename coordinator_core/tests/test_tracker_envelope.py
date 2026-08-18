"""
coordinator_core.tests.test_tracker_envelope — C4 shard-to-envelope fold
tests.

Purpose: cover `tracker_envelope.build_ingest_envelope` — the fold from our
per-machine JSONL shards into ONE JSON-serializable object for cockpit's
`ingest_emission`.

Coverage requirements (plan docs/plans/2026-08-18-sat-07-tier-a-wiring.md
§ Acceptance Criteria, § Task C4):
  AC12 — every shard folds into one envelope object (multi-shard fixture).
  AC13 — every item in the envelope carries `fold_membership_wire`'s
         materialized `projects: string[]`, with `"unassigned"` present for
         a zero-real-edge item and never an empty array.

Also covers the JSON-serializability of the returned envelope, since that
is the entire point of folding JSONL shards into one object for cockpit's
`ingest_emission`.

Spec backlink: docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Task C4,
§ Acceptance Criteria AC12/AC13.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core import tracker_entities, tracker_store
from coordinator_core.tracker_entities import (
    RESERVED_PROJECT_ID,
    emit_item_created,
    emit_item_project_added,
    emit_project_created,
    mint_item_id,
)
from coordinator_core.tracker_envelope import (
    TRACKER_EVENTS_KEY,
    TRACKER_ITEMS_KEY,
    build_ingest_envelope,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Same real-git-repo requirement as test_tracker_projection.py: append_event's
# locked_rmw resolves its lock directory via real `git rev-parse
# --git-common-dir`, so a bare non-git tmp_path fails before this module's
# own fold logic runs.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_git_repo(root: Path) -> Path:
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
    _git("config", "user.email", "tracker-envelope-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Envelope Test")
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
    emit_item_created(
        item_id,
        title=title,
        body=body,
        created_at="2026-08-05T10:00:00.000000Z",
        repo_root=repo_root,
    )
    return item_id


def test_ac12_multiple_shards_fold_into_one_envelope_object(repo_root, monkeypatch):
    # Two distinct machines writing to two distinct shard files (mirrors
    # test_tracker_store.py's multi-machine pattern) -- the envelope must
    # be the fold of both, not just whichever shard happens to be read
    # first.
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-a")
    on_a = _make_item(repo_root, title="OnMachineA", body="from shard a")

    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "machine-b")
    on_b = _make_item(repo_root, title="OnMachineB", body="from shard b")

    shard_a = repo_root / "state" / "sovereign-tracker" / "events.machine-a.jsonl"
    shard_b = repo_root / "state" / "sovereign-tracker" / "events.machine-b.jsonl"
    assert shard_a.is_file()
    assert shard_b.is_file()

    envelope = build_ingest_envelope(repo_root)

    assert set(envelope.keys()) == {TRACKER_ITEMS_KEY, TRACKER_EVENTS_KEY}
    item_ids = {item["id"] for item in envelope[TRACKER_ITEMS_KEY]}
    assert item_ids == {on_a, on_b}
    event_item_ids = {
        event.get("item_id")
        for event in envelope[TRACKER_EVENTS_KEY]
        if event.get("kind") == "item_created"
    }
    assert event_item_ids == {on_a, on_b}


def test_ac13_zero_real_edge_item_carries_unassigned(repo_root):
    bare_item = _make_item(repo_root, title="Bare", body="no project mutation ever")
    emit_project_created("proj-alpha", name="Alpha", repo_root=repo_root)
    with_edge = _make_item(repo_root, title="WithEdge", body="has a real project")
    emit_item_project_added(with_edge, "proj-alpha", repo_root=repo_root)

    envelope = build_ingest_envelope(repo_root)

    by_id = {item["id"]: item for item in envelope[TRACKER_ITEMS_KEY]}
    assert by_id[bare_item]["projects"] == [RESERVED_PROJECT_ID]
    assert by_id[with_edge]["projects"] == ["proj-alpha"]
    # Never an empty array for any item.
    assert all(item["projects"] for item in envelope[TRACKER_ITEMS_KEY])


def test_envelope_round_trips_through_json(repo_root):
    _make_item(repo_root, title="Roundtrip", body="must serialize cleanly")

    envelope = build_ingest_envelope(repo_root)
    serialized = json.dumps(envelope)
    deserialized = json.loads(serialized)

    assert deserialized == envelope
