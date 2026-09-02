"""test_publish_door_name_map — the `coordinator-publish` route emits the
door-facing published-name map, and an emitted map reaches the destination.

REGRESSION NET for a measured production defect. The door
(`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::resolve_target_path`)
consults `coordinator/bin/published-name-map.json` to translate a forwarder's
source-spelled target into the basename publish actually shipped. That map was
absent from the live mirror, and four installed forwarders named a target the
published engine could not serve — a fail-loud 127 on every box diverted to it.

Two independent causes, one net each here:

  1. ROUTE GAP. `emit_published_name_map` was wired into
     `coordinator_core/percolate/round.py::run_round` and nowhere else. The
     other sanctioned route, `coordinator/bin/publish.py`
     (`coordinator-publish`), never called it — and the percolate skill
     routinely names that route for several registered rows against one
     mirror, so it is the normal shape, not an edge case. Which command an
     operator typed decided whether a door-facing artifact existed at all.

  2. SURVIVAL. `publish_sync._sweep_mirror_top_level_orphans` (opt-in, and ON
     for a row that owns its destination subdirectory outright, which
     `coordinator/bin` does) deletes every destination top-level file the
     source does not have. A generated artifact is absent from the source by
     construction, so a map emitted by one route was reaped by the next run of
     the other. The fix is not an exemption but ordering: this route emits
     AFTER the sweep has already run against the staging tree it is about to
     swap in.

These tests are STUB-ONLY — no git spawn, no publish round, no engine phase.

Run: python -m pytest coordinator/bin/tests/test_publish_door_name_map.py -q
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from dataclasses import dataclass
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "coordinator" / "lib") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "coordinator" / "lib"))

from coordinator_core.percolate.rewrite_basename import (  # noqa: E402
    PUBLISHED_NAME_MAP_BASENAME,
    RenameManifest,
    RenameRecord,
)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_door_name_map_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@dataclass
class _Target:
    """The two fields the door-emission leg reads off a `ResolvedTarget`.

    A stub rather than the real dataclass because this leg's contract is
    exactly `dest_dir` (where the row LANDS, which decides whether the map
    belongs to it) plus a name for reporting — constructing a full
    `ResolvedTarget` would pin fields this behaviour does not depend on.
    """

    name: str
    dest_dir: Path


def _mirror_with_row(tmp_path: Path, dest_prefix: str) -> "tuple[Path, _Target]":
    """A destination repo root (a bare `.git` marker is all `_dest_prefix_for`
    walks for) holding one row's destination subtree."""
    repo_root = tmp_path / "mirror"
    (repo_root / ".git").mkdir(parents=True)
    dest_dir = repo_root / dest_prefix
    dest_dir.mkdir(parents=True)
    return repo_root, _Target(name="a-row", dest_dir=dest_dir)


def _staging(tmp_path: Path, name: str = "staging") -> _Target:
    staging_dir = tmp_path / name
    staging_dir.mkdir(parents=True, exist_ok=True)
    return _Target(name="a-row", dest_dir=staging_dir)


def _live_rename_records() -> "list[dict]":
    """The wire shape (`RenameManifest.as_records()`) this route receives back
    from its `post_rsync` phase, carrying the one live rename that proves the
    map is shipped rather than inferred: `probe-cwd-example-retrieval-repo-relevance.py`
    publishes under a name derivable from nothing in the source name."""
    return [
        {
            "old_path": "check-claude-klabauter-doctor-sentinel.sh",
            "new_path": "check-claude-klabauter-doctor-sentinel.py",
            "kind": "file",
        },
        {
            "old_path": "probe-cwd-example-retrieval-repo-relevance.py",
            "new_path": "probe-cwd-example-retrieval-repo-relevance.py",
            "kind": "file",
        },
        {
            "old_path": "resolve-claude-klabauter",
            "new_path": "resolve-claude-klabauter",
            "kind": "directory",
        },
    ]


# ---------------------------------------------------------------------------
# Cause 1 — the route gap.
# ---------------------------------------------------------------------------


def test_publish_route_emits_the_name_map_for_the_bin_row(tmp_path):
    """The `coordinator-publish` route writes the map into the staging tree it
    swaps in — keyed by SOURCE basename, valued by PUBLISHED basename, file
    renames only."""
    _, target = _mirror_with_row(tmp_path, "coordinator/bin")
    sync_target = _staging(tmp_path)

    emission = publish.emit_door_name_map_for_publish_row(
        target, sync_target, _live_rename_records()
    )

    map_path = sync_target.dest_dir / PUBLISHED_NAME_MAP_BASENAME
    assert emission.emitted, emission.render()
    assert map_path.is_file(), (
        "the publish route produced no door name map — an installed forwarder "
        "asking for a renamed target has nothing to translate through"
    )
    assert json.loads(map_path.read_text(encoding="utf-8")) == {
        "check-claude-klabauter-doctor-sentinel.sh": "check-claude-klabauter-doctor-sentinel.py",
        "probe-cwd-example-retrieval-repo-relevance.py": (
            "probe-cwd-example-retrieval-repo-relevance.py"
        ),
    }


def test_publish_route_shares_the_round_drivers_emitter(tmp_path):
    """ONE call path, not two: the byte the publish route writes and the byte
    `run_round` writes come out of the same function, so the two routes cannot
    drift into disagreeing about the map's shape."""
    _, target = _mirror_with_row(tmp_path, "coordinator/bin")
    via_publish = _staging(tmp_path, "via-publish")
    via_round = _staging(tmp_path, "via-round")

    publish.emit_door_name_map_for_publish_row(
        target, via_publish, _live_rename_records()
    )
    from coordinator_core.percolate import rewrite_basename

    rewrite_basename.emit_door_name_map_for_row(
        via_round.dest_dir,
        "coordinator/bin",
        RenameManifest(
            [RenameRecord(**record) for record in _live_rename_records()]
        ),
    )

    assert (via_publish.dest_dir / PUBLISHED_NAME_MAP_BASENAME).read_bytes() == (
        via_round.dest_dir / PUBLISHED_NAME_MAP_BASENAME
    ).read_bytes()


def test_publish_route_emits_nothing_for_a_row_that_is_not_the_door(tmp_path):
    """A map under a `lib/`/doc/engine row would name a binding nothing reads.
    The skip is REPORTED, never silent."""
    _, target = _mirror_with_row(tmp_path, "coordinator_core")
    sync_target = _staging(tmp_path)

    emission = publish.emit_door_name_map_for_publish_row(
        target, sync_target, _live_rename_records()
    )

    assert not emission.emitted
    assert not (sync_target.dest_dir / PUBLISHED_NAME_MAP_BASENAME).exists()
    assert "not the door's bin destination" in emission.render()


def test_the_emission_is_wired_into_process_target_after_post_rsync():
    """The helper existing is not the fix — being CALLED on the live route is.
    Ordering matters too: `post_rsync` is the earliest point this route holds a
    real rename manifest, so an emission before it would ship an empty map."""
    source = inspect.getsource(publish.process_target)
    assert "emit_door_name_map_for_publish_row(" in source, (
        "process_target no longer emits the door name map — the publish route "
        "is back to shipping a mirror the door cannot read"
    )
    assert source.index("dispatch_percolate_post_rsync(") < source.index(
        "emit_door_name_map_for_publish_row("
    ), "the map must be emitted after post_rsync, which is what produces the manifest"


def test_the_engine_row_vintage_is_emitted_beside_its_stamp():
    """`_engine_published_at` is the name map's sibling in the same class of
    door-facing emission, and was equally absent from the mirror: this route
    wrote `_engine_stamp` and no vintage at all."""
    source = inspect.getsource(publish.run_pre_sync_gates)
    assert "_emit_engine_row_published_at(" in source
    assert source.index("_ENGINE_STAMP_FILENAME") < source.index(
        "_emit_engine_row_published_at("
    )


# ---------------------------------------------------------------------------
# Cause 2 — survival into the destination.
# ---------------------------------------------------------------------------


def test_an_emitted_map_survives_the_top_level_orphan_sweep(tmp_path):
    """END STATE, not intent. The row's top-level orphan sweep deletes every
    destination file absent from the source, and a generated artifact is absent
    from the source by construction — so a map emitted by the round driver was
    reaped by the next `coordinator-publish` run.

    Exercised through the real sync engine at the real ordering: sweep first
    (against the staging tree this run swaps in), emission after."""
    from percolate.ignore import PercolateIgnoreMatcher
    from percolate.publish_sync import sync_mirror

    source_dir = tmp_path / "restricted-source"
    (source_dir / "sub").mkdir(parents=True)
    (source_dir / "sub" / "payload.py").write_text("x\n", encoding="utf-8")
    (source_dir / "a-cli.py").write_text("x\n", encoding="utf-8")

    _, target = _mirror_with_row(tmp_path, "coordinator/bin")
    sync_target = _Target(name="a-row", dest_dir=tmp_path / "staging")
    sync_target.dest_dir.mkdir()
    carried_over = sync_target.dest_dir / PUBLISHED_NAME_MAP_BASENAME
    carried_over.write_text('{"stale": "stale"}\n', encoding="utf-8")

    sync_mirror(
        source_dir,
        sync_target.dest_dir,
        PercolateIgnoreMatcher([]),
        dry_run=False,
        sweep_top_level_orphans=True,
        renamed_file_names=frozenset(),
    )
    assert not carried_over.exists(), (
        "fixture no longer exercises the hazard — the orphan sweep did not reap "
        "the map, so this test would pass without the emission"
    )

    publish.emit_door_name_map_for_publish_row(
        target, sync_target, _live_rename_records()
    )

    assert carried_over.is_file(), (
        "the map did not survive the publish route into the tree that gets "
        "swapped into the destination"
    )
    assert "stale" not in json.loads(carried_over.read_text(encoding="utf-8")), (
        "a prior pass's bindings survived unrefreshed — the map must always be "
        "THIS pass's set, never a stale binding"
    )


def test_the_map_is_declared_as_deliberately_unscanned():
    """The map cannot have been visited by the sweep that produces the manifest
    it is built from, so the end-of-run unscanned-published check must be told
    once, with a reason, rather than failing every bin-row publish."""
    exceptions = publish._load_unscanned_exceptions()
    assert f"coordinator/bin/{PUBLISHED_NAME_MAP_BASENAME}" in exceptions
