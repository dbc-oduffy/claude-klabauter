"""Acceptance battery, part 2 — THE SPINOFF WALL (AC5) and archive-invariance
(AC10).

AC5 is the load-bearing test of the whole priority-ledger plan: the plan's
anti-scope names spinoff inheritance as the rule most likely to be
implemented backwards, and requires a red test, not a sentence, to guard it.
``forked_from`` and ``origin_handoff`` are REAL lineage edges
(``origin_handoff`` is registered in ``dag.EDGE_KIND_META``) — exactly why an
implementer told to "walk the lineage DAG" traverses them and inherits
straight across a fork, believing it rode existing structure. The priority
walk traverses ``predecessor`` (+ ``additional_predecessors``) ONLY; every
other lineage-shaped field is a non-edge for this resolver.

AC10 guards the reason the resolver reads BOTH live and archived ledger
entries: an archival ``git mv`` of an explicit-priority ancestor must not
silently drop a live descendant's inherited priority.

Fixture/style conventions mirror ``test_priority_resolve.py``'s
``_write_node``/``_ledger`` helpers exactly (see that module for the
canonical shape).

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § AC5, § AC10.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.emit.priority_resolve import resolve_priority

from coordinator_core.ops.emit.tests.conftest import _ledger, _write_node  # noqa: F401


@pytest.fixture()
def node_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    return d


def test_spinoff_with_forked_from_and_origin_handoff_does_not_inherit(node_dir: Path):
    _write_node(node_dir, "parent.md", handoff_id="parent_id", predecessor=None)
    spinoff_path = _write_node(
        node_dir,
        "spinoff.md",
        handoff_id="spinoff_id",
        predecessor=None,
        forked_from="parent.md",
        origin_handoff="parent.md",
    )

    ledger = _ledger(parent_id="urgent")

    result = resolve_priority(str(spinoff_path), "spinoff_id", ledger_entries=ledger)

    assert result == {"effective_priority": None, "origin": "none", "source_id": None}
    assert result["effective_priority"] != "urgent"


def test_spinoff_unset_does_not_resolve_through_forked_from_parent(node_dir: Path):
    _write_node(node_dir, "parent2.md", handoff_id="parent2_id", predecessor=None)
    spinoff_path = _write_node(
        node_dir,
        "spinoff2.md",
        handoff_id="spinoff2_id",
        predecessor=None,
        forked_from="parent2.md",
    )

    ledger = _ledger(parent2_id="high")

    result = resolve_priority(str(spinoff_path), "spinoff2_id", ledger_entries=ledger)

    assert result["effective_priority"] is None
    assert result["origin"] == "none"
    assert result["source_id"] is None


NON_EDGE_FIELDS = [
    "forked_from",
    "origin_handoff",
    "origin_plan_id",
    "origin_goal_id",
    "origin_session",
    "supersedes",
]


@pytest.mark.parametrize("non_edge_field", NON_EDGE_FIELDS)
def test_non_edge_lineage_field_is_never_traversed(node_dir: Path, non_edge_field: str):
    ancestor_id = f"ancestor_via_{non_edge_field}"
    _write_node(node_dir, "ancestor.md", handoff_id=ancestor_id, predecessor=None)
    node_path = _write_node(
        node_dir,
        "descendant.md",
        handoff_id="descendant_id",
        predecessor=None,
        **{non_edge_field: "ancestor.md"},
    )

    ledger = _ledger(**{ancestor_id: "urgent"})

    result = resolve_priority(str(node_path), "descendant_id", ledger_entries=ledger)

    assert result == {"effective_priority": None, "origin": "none", "source_id": None}


def test_resolution_unchanged_when_explicit_ancestor_archives(tmp_path: Path):
    state_root = tmp_path / "state"
    live_handoffs = state_root / "handoffs"
    live_handoffs.mkdir(parents=True)

    _write_node(live_handoffs, "ancestor.md", handoff_id="ancestor_id", predecessor=None)
    descendant_path = _write_node(
        live_handoffs, "descendant.md", handoff_id="descendant_id", predecessor="ancestor.md"
    )

    ledger = _ledger(ancestor_id="high")

    before = resolve_priority(str(descendant_path), "descendant_id", ledger_entries=ledger)
    assert before == {"effective_priority": "high", "origin": "inherited", "source_id": "ancestor_id"}

    archive_dir = tmp_path / "archive" / "handoffs" / "2026-06"
    archive_dir.mkdir(parents=True)
    shutil.move(str(live_handoffs / "ancestor.md"), str(archive_dir / "ancestor.md"))

    after = resolve_priority(str(descendant_path), "descendant_id", ledger_entries=ledger)

    assert after == before
