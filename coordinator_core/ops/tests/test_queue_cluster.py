"""
coordinator_core.ops.tests.test_queue_cluster -- op-level coverage for 'queue.cluster'.

Purpose: exercise the queue.cluster handler directly (registry lookup is out
of scope for this chunk -- registration is a dedicated later chunk's job)
across all three queue families, the explicit signal-set parameter, the
caller-supplied floor, the response envelope's `id` field, and a value-pin
over the signal enum (mirrors coordinator_core/clustering/tests/
test_candidates_pin.py's STOP_WORDS pin pattern).

Spec backlink: pln-queue-triage-terminus-ops-clus-043c40 § C3
"""

from __future__ import annotations

import subprocess

import coordinator_core.ops  # noqa: F401 -- populates _REGISTRY for the eagerly-wired ops
import coordinator_core.ops.queue_cluster  # noqa: F401 -- registration is a later chunk's job;
# this module is not yet in _EAGER_OP_MODULES, so import it directly to fire its @register_op.

from coordinator_core.ipc import _REGISTRY

assert "queue.cluster" in _REGISTRY, (
    "import guard failed: 'queue.cluster' not in _REGISTRY -- "
    "coordinator_core.ops.queue_cluster @register_op did not fire"
)

from coordinator_core.ops.queue_cluster import (  # noqa: E402
    ALL_SIGNALS,
    SIGNAL_DIRECTORY,
    SIGNAL_KEYWORD,
    SIGNAL_TAG,
    InvalidSignalError,
    _handler,
    cluster_records,
)

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Repo fixture -- a bare tmp_path, no git required (records_query spawns none)
# ---------------------------------------------------------------------------


def _seed_debt(root, name: str, *, title: str, tags=None) -> None:
    path = root / "state" / "debt-backlog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "created: 2026-01-01",
        f'title: "{title}"',
        'body: "Test body."',
        "status: open",
        'source: "daily-review/2026-01-01"',
        'risk: "Low."',
        'proposed_action: "None required."',
    ]
    if tags:
        tag_list = ", ".join(f'"{t}"' for t in tags)
        lines.append(f"tags: [{tag_list}]")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_improvement(root, name: str, *, title: str, tags=None) -> None:
    path = root / "state" / "improvement-queue" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "created: 2026-01-01",
        f'title: "{title}"',
        'body: "Test body."',
        "status: open",
        'surface: "test-surface"',
        'proposed_action: "None required."',
        'from_repo: "claude-klabauter"',
        "change_kind: doc-edit",
    ]
    if tags:
        tag_list = ", ".join(f'"{t}"' for t in tags)
        lines.append(f"tags: [{tag_list}]")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_bug(root, name: str, *, title: str, tags=None) -> None:
    path = root / "state" / "bug-backlog" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "created: 2026-01-01",
        f'title: "{title}"',
        'body: "Test body."',
        "status: open",
        'surface: "test-surface"',
        "severity: low",
    ]
    if tags:
        tag_list = ", ".join(f'"{t}"' for t in tags)
        lines.append(f"tags: [{tag_list}]")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_SEED_FN = {
    "debt-backlog": _seed_debt,
    "improvement-queue": _seed_improvement,
    "bug-backlog": _seed_bug,
}


def _init_repo(root) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Signal enum pin -- same mechanical teeth as C2's STOP_WORDS pin
# ---------------------------------------------------------------------------


def test_signal_enum_pinned() -> None:
    """The signal enum's names AND order are frozen.

    DoE's `/debt-triage` Step 6b suppresses the "directory" signal by literal
    string comparison against this exact value. A rename or re-case here does
    not error on their side -- it silently stops matching, and the next
    triage run hands their ceremony one un-suppressed, oversized cluster as a
    themed-baton candidate instead of the disposal their policy intended. If
    the signal set genuinely needs to change, coordinate it with
    claude-central-em via a cross-repo memo first -- do not rename and ship.
    """
    assert ALL_SIGNALS == ("tag", "directory", "keyword"), (
        "queue.cluster's signal enum changed -- this is a cross-repo contract "
        "break for claude-central-em's /debt-triage Step 6b, which string-"
        "matches on 'directory' literally. Coordinate via cross-repo memo "
        "before changing this value, don't just rename and ship."
    )
    assert SIGNAL_TAG == "tag"
    assert SIGNAL_DIRECTORY == "directory"
    assert SIGNAL_KEYWORD == "keyword"


# ---------------------------------------------------------------------------
# Family coverage -- all three families, default signal set
# ---------------------------------------------------------------------------


class TestAllThreeFamilies:
    def test_debt_backlog_clusters_by_tag(self, tmp_path) -> None:
        for i in range(3):
            _seed_debt(tmp_path, f"entry-{i}.yaml", title=f"Debt entry {i}", tags=["widget"])
        result = _handler({"family": "debt-backlog"}, repo_root=tmp_path)
        tag_clusters = [c for c in result if c["signal"] == "tag"]
        assert len(tag_clusters) == 1
        assert tag_clusters[0]["value"] == "widget"
        assert len(tag_clusters[0]["items"]) == 3

    def test_improvement_queue_clusters_by_tag(self, tmp_path) -> None:
        for i in range(3):
            _seed_improvement(tmp_path, f"entry-{i}.yaml", title=f"Improvement {i}", tags=["gadget"])
        result = _handler({"family": "improvement-queue"}, repo_root=tmp_path)
        tag_clusters = [c for c in result if c["signal"] == "tag"]
        assert len(tag_clusters) == 1
        assert tag_clusters[0]["value"] == "gadget"

    def test_bug_backlog_clusters_by_tag(self, tmp_path) -> None:
        for i in range(3):
            _seed_bug(tmp_path, f"entry-{i}.yaml", title=f"Bug {i}", tags=["sprocket"])
        result = _handler({"family": "bug-backlog"}, repo_root=tmp_path)
        tag_clusters = [c for c in result if c["signal"] == "tag"]
        assert len(tag_clusters) == 1
        assert tag_clusters[0]["value"] == "sprocket"


# ---------------------------------------------------------------------------
# Envelope shape -- id/path/title present, empty result is [] never None
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    def test_item_carries_id_derived_from_filename_stem(self, tmp_path) -> None:
        for i in range(3):
            _seed_debt(tmp_path, f"widget-entry-{i}.yaml", title=f"Widget entry {i}", tags=["shared"])
        result = _handler({"family": "debt-backlog"}, repo_root=tmp_path)
        cluster = next(c for c in result if c["signal"] == "tag")
        ids = {item["id"] for item in cluster["items"]}
        assert ids == {"widget-entry-0", "widget-entry-1", "widget-entry-2"}
        for item in cluster["items"]:
            assert set(item.keys()) == {"id", "path", "title"}

    def test_empty_family_returns_empty_list_not_none(self, tmp_path) -> None:
        (tmp_path / "state" / "debt-backlog").mkdir(parents=True)
        result = _handler({"family": "debt-backlog"}, repo_root=tmp_path)
        assert result == []
        assert result is not None


class TestCommonDirRepoRootShape:
    """Regression for the 2026-07-23 silent-empty-result bug.

    ``queue.cluster`` is registered ``"common_dir"`` in
    ``coordinator_core.op_scopes._OP_KEY_SCOPE``, so the IPC engine hands its
    handler ``git_common_dir(caller_worktree)`` (``<worktree>/.git``), never
    the worktree root. This exercises exactly that dispatch shape rather than
    the worktree-root shape every other test in this file uses.
    """

    def test_handler_with_git_common_dir_finds_records(self, tmp_path) -> None:
        _init_repo(tmp_path)
        for i in range(3):
            _seed_debt(tmp_path, f"entry-{i}.yaml", title=f"Debt entry {i}", tags=["widget"])
        common_dir = tmp_path / ".git"
        assert common_dir.is_dir()  # sanity: standard (non-worktree) layout

        result = _handler({"family": "debt-backlog"}, repo_root=common_dir)
        tag_clusters = [c for c in result if c["signal"] == "tag"]
        assert len(tag_clusters) == 1
        assert tag_clusters[0]["value"] == "widget"
        assert len(tag_clusters[0]["items"]) == 3


# ---------------------------------------------------------------------------
# Signal-set parameter -- default == today's three-signal behavior;
# a caller-supplied subset suppresses the excluded signals entirely.
# ---------------------------------------------------------------------------


class TestSignalSetParameter:
    def test_default_signal_set_runs_all_three(self) -> None:
        result = cluster_records(
            [
                {"path": f"state/debt-backlog/e{i}.yaml", "frontmatter": {"title": "Shared topic keyword cluster", "tags": ["onetag"]}}
                for i in range(3)
            ]
        )
        signals_present = {c["signal"] for c in result}
        assert signals_present == {"tag", "directory", "keyword"}, (
            "default (no signals param) must run all three signals -- "
            "today's fixed-order behavior, byte-identical"
        )

    def test_caller_supplied_subset_suppresses_directory_signal(self, tmp_path) -> None:
        records = [
            {"path": f"state/debt-backlog/e{i}.yaml", "frontmatter": {"title": "Shared topic keyword cluster", "tags": ["onetag"]}}
            for i in range(3)
        ]
        result = cluster_records(records, signals=[SIGNAL_TAG, SIGNAL_KEYWORD])
        signals_present = {c["signal"] for c in result}
        assert "directory" not in signals_present
        assert signals_present <= {"tag", "keyword"}

    def test_unknown_signal_raises(self) -> None:
        try:
            cluster_records([], signals=["bogus"])
            raised = False
        except InvalidSignalError:
            raised = True
        assert raised, "an unrecognized signal name must raise InvalidSignalError"


# ---------------------------------------------------------------------------
# Caller-supplied floor -- default MIN_CLUSTER_SIZE == 3; a lower floor
# surfaces smaller clusters when explicitly requested.
# ---------------------------------------------------------------------------


class TestFloorParameter:
    def test_default_floor_suppresses_pair(self) -> None:
        records = [
            {"path": "state/debt-backlog/a.yaml", "frontmatter": {"title": "A", "tags": ["pair"]}},
            {"path": "state/debt-backlog/b.yaml", "frontmatter": {"title": "B", "tags": ["pair"]}},
        ]
        result = cluster_records(records)
        assert result == []

    def test_lower_floor_surfaces_pair_when_explicitly_requested(self) -> None:
        records = [
            {"path": "state/debt-backlog/a.yaml", "frontmatter": {"title": "A", "tags": ["pair"]}},
            {"path": "state/debt-backlog/b.yaml", "frontmatter": {"title": "B", "tags": ["pair"]}},
        ]
        result = cluster_records(records, signals=[SIGNAL_TAG], min_cluster_size=2)
        assert len(result) == 1
        assert result[0]["value"] == "pair"
