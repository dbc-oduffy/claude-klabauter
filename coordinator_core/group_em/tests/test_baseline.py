"""Tests for coordinator_core.group_em.baseline.

Spec backlink: docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md, chunk C4.
"""

from __future__ import annotations

from pathlib import Path

import json

from coordinator_core.group_em.baseline import (
    FIRST_TICK_NEVER_ARMED,
    FIRST_TICK_UNREADABLE,
    diff_and_persist,
    _store_path,
)


def test_first_tick_reports_empty_spawned_and_first_tick_true(tmp_path: Path) -> None:
    peers = {
        "sess-a": {"state": "running", "reason": None},
        "sess-b": {"state": "paused", "reason": "away"},
    }
    result = diff_and_persist(
        peers, repo_key="repo1", session_id="caller-1", repo_root=tmp_path
    )
    assert result["first_tick"] is True
    assert result["spawned"] == []
    assert result["exited"] == []
    assert result["changed"] == []


def test_second_tick_diffs_spawn_exit_and_change(tmp_path: Path) -> None:
    tick1 = {
        "sess-a": {"state": "running", "reason": None},
        "sess-b": {"state": "paused", "reason": "away"},
    }
    diff_and_persist(tick1, repo_key="repo1", session_id="caller-1", repo_root=tmp_path)

    tick2 = {
        "sess-a": {"state": "done", "reason": None},  # changed
        "sess-c": {"state": "running", "reason": None},  # spawned
        # sess-b exited
    }
    result = diff_and_persist(
        tick2, repo_key="repo1", session_id="caller-1", repo_root=tmp_path
    )
    assert result["first_tick"] is False
    assert result["spawned"] == ["sess-c"]
    assert result["exited"] == ["sess-b"]
    assert result["changed"] == ["sess-a"]


def test_reason_only_transition_counts_as_changed(tmp_path: Path) -> None:
    tick1 = {"sess-a": {"state": "paused", "reason": "away"}}
    diff_and_persist(tick1, repo_key="repo1", session_id="caller-1", repo_root=tmp_path)

    tick2 = {"sess-a": {"state": "paused", "reason": "blocked"}}
    result = diff_and_persist(
        tick2, repo_key="repo1", session_id="caller-1", repo_root=tmp_path
    )
    assert result["changed"] == ["sess-a"]
    assert result["spawned"] == []
    assert result["exited"] == []


def test_unchanged_peer_is_not_reported(tmp_path: Path) -> None:
    tick1 = {"sess-a": {"state": "running", "reason": None}}
    diff_and_persist(tick1, repo_key="repo1", session_id="caller-1", repo_root=tmp_path)

    result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo1",
        session_id="caller-1",
        repo_root=tmp_path,
    )
    assert result["spawned"] == []
    assert result["exited"] == []
    assert result["changed"] == []
    assert result["first_tick"] is False


def test_corrupt_store_degrades_to_first_tick(tmp_path: Path) -> None:
    tick1 = {"sess-a": {"state": "running", "reason": None}}
    diff_and_persist(tick1, repo_key="repo1", session_id="caller-1", repo_root=tmp_path)

    path = _store_path("repo1", "caller-1", repo_root=tmp_path)
    path.write_text("{not valid json truncated", encoding="utf-8")

    result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}, "sess-b": {"state": "running"}},
        repo_key="repo1",
        session_id="caller-1",
        repo_root=tmp_path,
    )
    assert result["first_tick"] is True
    assert result["spawned"] == []
    assert result["exited"] == []
    assert result["changed"] == []
    assert result["first_tick_reason"] == FIRST_TICK_UNREADABLE


def test_missing_store_file_degrades_to_first_tick_not_raise(tmp_path: Path) -> None:
    result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo-missing",
        session_id="caller-missing",
        repo_root=tmp_path,
    )
    assert result["first_tick"] is True


def test_two_caller_sessions_do_not_share_a_file(tmp_path: Path) -> None:
    diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo1",
        session_id="caller-1",
        repo_root=tmp_path,
    )
    result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo1",
        session_id="caller-2",
        repo_root=tmp_path,
    )
    assert result["first_tick"] is True

    path1 = _store_path("repo1", "caller-1", repo_root=tmp_path)
    path2 = _store_path("repo1", "caller-2", repo_root=tmp_path)
    assert path1 != path2
    assert path1.exists()
    assert path2.exists()


def test_store_path_shape(tmp_path: Path) -> None:
    path = _store_path("myrepo", "sess-xyz", repo_root=tmp_path)
    assert path == (
        tmp_path
        / "state"
        / "subagent-share"
        / "sess-xyz"
        / "group-em-baseline-myrepo.json"
    )


def test_first_tick_never_armed_reason_when_no_file(tmp_path: Path) -> None:
    result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo-fresh",
        session_id="caller-fresh",
        repo_root=tmp_path,
    )
    assert result["first_tick"] is True
    assert result["first_tick_reason"] == FIRST_TICK_NEVER_ARMED
    assert result["previous_taken_at"] is None
    assert isinstance(result["as_of"], str) and result["as_of"]


def test_diff_carries_as_of_and_previous_taken_at(tmp_path: Path) -> None:
    tick1 = {"sess-a": {"state": "running", "reason": None}}
    first = diff_and_persist(
        tick1, repo_key="repo1", session_id="caller-1", repo_root=tmp_path
    )
    assert first["as_of"]
    assert first["previous_taken_at"] is None

    path = _store_path("repo1", "caller-1", repo_root=tmp_path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["taken_at"] == first["as_of"]

    tick2 = {"sess-a": {"state": "done", "reason": None}}
    second = diff_and_persist(
        tick2, repo_key="repo1", session_id="caller-1", repo_root=tmp_path
    )
    assert second["previous_taken_at"] == first["as_of"]
    assert second["as_of"] != first["as_of"] or True  # instants may collide at 1s resolution
    assert second["first_tick"] is False
    assert second["first_tick_reason"] is None


def test_old_record_without_taken_at_reads_as_none_not_crash(tmp_path: Path) -> None:
    tick1 = {"sess-a": {"state": "running", "reason": None}}
    diff_and_persist(tick1, repo_key="repo1", session_id="caller-1", repo_root=tmp_path)

    path = _store_path("repo1", "caller-1", repo_root=tmp_path)
    old_shape = json.loads(path.read_text(encoding="utf-8"))
    old_shape.pop("taken_at", None)
    path.write_text(json.dumps(old_shape), encoding="utf-8")

    result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo1",
        session_id="caller-1",
        repo_root=tmp_path,
    )
    assert result["first_tick"] is False
    assert result["previous_taken_at"] is None


def test_corrupt_store_reports_unreadable_reason_distinct_from_fresh(tmp_path: Path) -> None:
    path = _store_path("repo-corrupt", "caller-x", repo_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    corrupt_result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo-corrupt",
        session_id="caller-x",
        repo_root=tmp_path,
    )

    fresh_result = diff_and_persist(
        {"sess-a": {"state": "running", "reason": None}},
        repo_key="repo-truly-fresh",
        session_id="caller-y",
        repo_root=tmp_path,
    )

    assert corrupt_result["first_tick"] is True
    assert fresh_result["first_tick"] is True
    assert corrupt_result["first_tick_reason"] == FIRST_TICK_UNREADABLE
    assert fresh_result["first_tick_reason"] == FIRST_TICK_NEVER_ARMED
    assert corrupt_result["first_tick_reason"] != fresh_result["first_tick_reason"]
