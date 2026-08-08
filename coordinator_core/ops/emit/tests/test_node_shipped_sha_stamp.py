"""Unit tests for _stamp_node_shipped_sha — emit-path shipped_sha verification for DAG nodes.

Purpose: verify that _stamp_node_shipped_sha correctly stamps verified SHAs, degrades all to
null when origin/main is unreachable, and leaves no stray shipped_in key on node records.

Mirrors the offline-degrade and ancestor-verified-SHA contract that _stamp_shipped_sha enforces
for handoffs, adapted to the bare-scalar RoadmapDagNode.shipped_sha field (no shipped_in dict).

No golden-fixture dependency — all records are synthetic in-memory objects.

Spec backlink: docs/plans/2026-07-06-roadmap-dag-emit-switch.md § C3 (D4 / F3)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import call, patch

import pytest

from coordinator_core.ops.emit.envelope import _stamp_node_shipped_sha

# ---------------------------------------------------------------------------
# RoadmapDagNode required field set (sans optional content_hash).
# Source: coordinator_core/ops/emit/_vendor/cockpit-contract/src/entities/roadmap-dag-node.ts
# ---------------------------------------------------------------------------
_ROADMAP_DAG_NODE_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "repo",
    "coordinator_root_path",
    "roadmap_id",
    "stub_id",
    "status",
    "sprint",
    "wave",
    "shipped_sha",
    "provenance",
})


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_node(shipped_sha: "str | None" = None) -> dict:
    """Build a minimal RoadmapDagNode-shaped record for stamp testing.

    Keys match the emitted shape produced by sections/roadmap_dag.py collect()
    after the kind tag is stripped by the envelope's place fn.  No shipped_in
    key — RoadmapDagNode has no such field (D4 F3).
    """
    return {
        "repo": "dbc-oduffy/claude-klabauter",
        "coordinator_root_path": ".",
        "roadmap_id": "test-roadmap",
        "stub_id": "test-stub",
        "status": "shipped" if shipped_sha else "in_progress",
        "sprint": "sprint-1",
        "wave": "wave-1",
        "shipped_sha": shipped_sha,
        "provenance": {
            "source_kind": "local_fs",
            "repo": "dbc-oduffy/claude-klabauter",
            "ref": None,
            "path": "",
            "observed_at": "2026-07-06T00:00:00Z",
            "derivation": "parsed",
        },
    }


# ---------------------------------------------------------------------------
# Core stamp behaviour
# ---------------------------------------------------------------------------

def test_node_stamp_populates_ancestor_verified_sha(tmp_path: Path) -> None:
    """Stamp writes back the SHA when it is confirmed an ancestor of origin/main."""
    sha = "abc1234567890abcdef"
    node = _make_node(shipped_sha=sha)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch("coordinator_core.ops.emit.envelope._classify_shas_on_origin_main", return_value={sha: True}),
    ):
        _stamp_node_shipped_sha([node], tmp_path)

    assert node["shipped_sha"] == sha, (
        "shipped_sha must be the verified SHA when it is an ancestor of origin/main"
    )


def test_node_stamp_sets_null_when_sha_not_on_main(tmp_path: Path) -> None:
    """Stamp writes null when the SHA is NOT an ancestor of origin/main (False result)."""
    sha = "notaonmain123abc"
    node = _make_node(shipped_sha=sha)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch("coordinator_core.ops.emit.envelope._classify_shas_on_origin_main", return_value={sha: False}),
    ):
        _stamp_node_shipped_sha([node], tmp_path)

    assert node["shipped_sha"] is None, (
        "shipped_sha must become null for a SHA that is not an ancestor of origin/main"
    )


def test_node_stamp_degrades_all_to_null_when_offline(tmp_path: Path) -> None:
    """All shipped_sha values become null when origin/main is unreachable after one fetch.

    Mirrors the offline-degrade contract of _stamp_shipped_sha on handoffs.
    """
    sha_a = "abc1234567890abcdef"
    sha_b = "def4567890abcdef123"
    node_a = _make_node(shipped_sha=sha_a)
    node_b = _make_node(shipped_sha=sha_b)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=False),
        patch("coordinator_core.ops.emit.envelope._fetch_origin_main"),
    ):
        _stamp_node_shipped_sha([node_a, node_b], tmp_path)

    assert node_a["shipped_sha"] is None, "offline: node_a shipped_sha must degrade to null"
    assert node_b["shipped_sha"] is None, "offline: node_b shipped_sha must degrade to null"


def test_node_stamp_degrade_on_exit2_equivalent(tmp_path: Path) -> None:
    """All shipped_sha degrade to null when the batch classifier returns None for a sha
    (exit-2 / bad-object equivalent) — the tri-state's third value, distinct from False."""
    sha = "abc1234567890abcdef"
    node = _make_node(shipped_sha=sha)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch("coordinator_core.ops.emit.envelope._classify_shas_on_origin_main", return_value={sha: None}),
    ):
        _stamp_node_shipped_sha([node], tmp_path)

    assert node["shipped_sha"] is None, (
        "exit-2 equivalent (classifier returns None for the sha) must degrade shipped_sha to null"
    )


def test_node_stamp_degrade_on_exit2_equivalent_is_all_or_nothing(tmp_path: Path) -> None:
    """A single None among several classified SHAs degrades EVERY node, not just the bad one —
    this is the property a naive per-sha 'None -> null, else use the map' rewrite would drop."""
    sha_shipped = "abc1234567890abcdef"
    sha_bad = "deadbeefnotarealobject"
    node_shipped = _make_node(shipped_sha=sha_shipped)
    node_bad = _make_node(shipped_sha=sha_bad)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch(
            "coordinator_core.ops.emit.envelope._classify_shas_on_origin_main",
            return_value={sha_shipped: True, sha_bad: None},
        ),
    ):
        _stamp_node_shipped_sha([node_shipped, node_bad], tmp_path)

    assert node_shipped["shipped_sha"] is None, (
        "one indeterminate sha in the batch must degrade ALL nodes, including the "
        "otherwise-verified sha_shipped node"
    )
    assert node_bad["shipped_sha"] is None


def test_node_stamp_mixed_shipped_and_not_shipped(tmp_path: Path) -> None:
    """Nodes with distinct SHAs are stamped independently: on-main=keep, off-main=null."""
    sha_shipped = "abc1234567890abcdef"
    sha_not_shipped = "def4567890abcdef123"
    node_a = _make_node(shipped_sha=sha_shipped)
    node_b = _make_node(shipped_sha=sha_not_shipped)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch(
            "coordinator_core.ops.emit.envelope._classify_shas_on_origin_main",
            return_value={sha_shipped: True, sha_not_shipped: False},
        ),
    ):
        _stamp_node_shipped_sha([node_a, node_b], tmp_path)

    assert node_a["shipped_sha"] == sha_shipped, "on-main SHA must be retained"
    assert node_b["shipped_sha"] is None, "off-main SHA must become null"


def test_node_stamp_deduplicates_sha_checks(tmp_path: Path) -> None:
    """Two nodes sharing the same shipped_sha trigger only one batch classify call, and that
    call is asked to classify exactly one distinct SHA."""
    sha = "abc1234567890abcdef"
    node_a = _make_node(shipped_sha=sha)
    node_b = _make_node(shipped_sha=sha)  # same SHA

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch(
            "coordinator_core.ops.emit.envelope._classify_shas_on_origin_main",
            return_value={sha: True},
        ) as mock_classify,
    ):
        _stamp_node_shipped_sha([node_a, node_b], tmp_path)

    assert mock_classify.call_count == 1, (
        "the whole node batch must resolve via exactly one classify call, not one per node"
    )
    (_, called_shas), _kwargs = mock_classify.call_args
    assert list(called_shas) == [sha], "duplicate SHAs must be deduplicated before the batch call"
    assert node_a["shipped_sha"] == sha
    assert node_b["shipped_sha"] == sha


def test_node_stamp_null_sha_nodes_skip_git_io(tmp_path: Path) -> None:
    """Nodes with shipped_sha=None are skipped; no git I/O is performed."""
    node = _make_node(shipped_sha=None)

    with patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable") as mock_git:
        _stamp_node_shipped_sha([node], tmp_path)

    mock_git.assert_not_called()
    assert node["shipped_sha"] is None


def test_node_stamp_empty_list_is_noop(tmp_path: Path) -> None:
    """An empty node list returns immediately without any git I/O."""
    with patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable") as mock_git:
        _stamp_node_shipped_sha([], tmp_path)

    mock_git.assert_not_called()


def test_node_stamp_one_fetch_attempt_when_initially_unreachable(tmp_path: Path) -> None:
    """origin/main is probed once; one fetch is attempted; second probe confirms unreachable."""
    sha = "abc1234567890abcdef"
    node = _make_node(shipped_sha=sha)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=False),
        patch("coordinator_core.ops.emit.envelope._fetch_origin_main") as mock_fetch,
    ):
        _stamp_node_shipped_sha([node], tmp_path)

    mock_fetch.assert_called_once()
    assert node["shipped_sha"] is None


# ---------------------------------------------------------------------------
# Key-set contract: no stray shipped_in key, exact RoadmapDagNode field set (D4 / F3)
# ---------------------------------------------------------------------------

def test_node_stamp_no_stray_shipped_in_key(tmp_path: Path) -> None:
    """After stamping, no shipped_in key appears on the node record.

    D4 / F3: preferred approach is bare-scalar stamp; shipped_in must NEVER appear.
    Zod .object() strips unknown keys silently — a stray shipped_in would pass Zod
    validation while the emitted disk-truth carries a non-contract field that rag ingests.
    """
    sha = "abc1234567890abcdef"
    node = _make_node(shipped_sha=sha)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch("coordinator_core.ops.emit.envelope._classify_shas_on_origin_main", return_value={sha: True}),
    ):
        _stamp_node_shipped_sha([node], tmp_path)

    assert "shipped_in" not in node, (
        "shipped_in must not appear on a stamped node — it is not a RoadmapDagNode contract field"
    )


def test_node_stamp_keys_match_roadmap_dag_node_field_set(tmp_path: Path) -> None:
    """Stamped node keys must equal the RoadmapDagNode field set (no extras beyond content_hash).

    Asserts:
      - All required RoadmapDagNode fields are present.
      - No unexpected non-contract fields exist (shipped_in, kind, or any other stray key).
      - content_hash is the only permitted optional field beyond the required set.
    """
    sha = "abc1234567890abcdef"
    node = _make_node(shipped_sha=sha)

    with (
        patch("coordinator_core.ops.emit.envelope._check_origin_main_reachable", return_value=True),
        patch("coordinator_core.ops.emit.envelope._classify_shas_on_origin_main", return_value={sha: True}),
    ):
        _stamp_node_shipped_sha([node], tmp_path)

    node_keys = set(node.keys())

    # All required fields must be present.
    missing = _ROADMAP_DAG_NODE_REQUIRED_FIELDS - node_keys
    assert not missing, f"missing required RoadmapDagNode fields after stamp: {missing}"

    # No non-contract keys beyond the optional content_hash.
    allowed = _ROADMAP_DAG_NODE_REQUIRED_FIELDS | {"content_hash"}
    unexpected = node_keys - allowed
    assert not unexpected, (
        f"unexpected non-contract fields on node after stamp: {unexpected} — "
        "Zod .object() would strip these silently, corrupting disk-truth"
    )
