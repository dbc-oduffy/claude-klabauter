"""Regression tests for the shared goals-log reader (``coordinator_core.goals.wire_read``).

Pins the preserved-failure-path contract this module exists to guarantee: the
os.scandir-before-glob probe, per-shard provenance pairing, malformed-line/non-dict-JSON
quarantine, the undecodable/unreadable-shard guard, legacy-row goal_id unification against
``goal_append._goal_id``, and the strict ``>`` declared_at tie-break. The reader itself
must stay policy-neutral — an unscannable root is reported back as ``unreadable_error``,
never raised (see ``coordinator_core/ops/emit/sections/goals.py`` for the emit-path raise
policy and its own ``test_unreadable_central_state_root_raises_not_zero_goals``).

Spec backlink: docs/plans/2026-07-25-day-goal-close-out-lifecycle.md § C1
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.goals.wire_read import read_and_collapse
from coordinator_core.ops.goal_append import _goal_id


def _write_shard(root: Path, machine: str, lines: list[str]) -> Path:
    shard = root / f"goals-log.{machine}.jsonl"
    shard.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return shard


def _record(**overrides) -> dict:
    base = {
        "goal_id": "wire-read-base",
        "repo": "test-org/test-repo",
        "coordinator_root_path": ".",
        "period": "day",
        "period_value": "2026-06-24",
        "declared_by_machine": "test-host",
        "declared_at": "2026-06-24T09:00:00Z",
        "text": "Base wire-read fixture goal.",
        "status": "active",
    }
    base.update(overrides)
    return base


def test_empty_state_root_returns_no_rows_no_error(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()

    result = read_and_collapse(root)

    assert result.rows == []
    assert result.unreadable_error is None


def test_missing_state_root_returns_no_rows_no_error(tmp_path: Path) -> None:
    root = tmp_path / "does-not-exist"

    result = read_and_collapse(root)

    assert result.rows == []
    assert result.unreadable_error is None


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_root_reports_error_does_not_raise(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_shard(root, "test-host", [json.dumps(_record())])

    original_mode = root.stat().st_mode
    os.chmod(root, 0o000)
    try:
        result = read_and_collapse(root)
    finally:
        os.chmod(root, original_mode)

    assert result.rows == []
    assert isinstance(result.unreadable_error, OSError)


def test_undecodable_shard_os_error_is_quarantined(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "state"
    root.mkdir()
    bad_shard = _write_shard(root, "bad-host", [json.dumps(_record(goal_id="bad-shard"))])
    good_shard = _write_shard(
        root, "good-host", [json.dumps(_record(goal_id="good-shard"))]
    )

    original_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == bad_shard:
            raise OSError("simulated unreadable shard")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    result = read_and_collapse(root)

    assert result.unreadable_error is None
    goal_ids = {row.goal_id for row in result.rows}
    assert goal_ids == {"good-shard"}
    assert good_shard in {row.shard_path for row in result.rows}


def test_undecodable_shard_unicode_decode_error_is_quarantined(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    bad_shard = _write_shard(root, "bad-host", [json.dumps(_record(goal_id="bad-shard"))])
    _write_shard(root, "good-host", [json.dumps(_record(goal_id="good-shard"))])

    original_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == bad_shard:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated bad bytes")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    result = read_and_collapse(root)

    assert result.unreadable_error is None
    goal_ids = {row.goal_id for row in result.rows}
    assert goal_ids == {"good-shard"}


def test_malformed_json_line_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_shard(
        root,
        "test-host",
        [
            "{not valid json",
            json.dumps(_record(goal_id="survives")),
        ],
    )

    result = read_and_collapse(root)

    assert [row.goal_id for row in result.rows] == ["survives"]


@pytest.mark.parametrize("payload", ["5", '"oops"', "[1, 2, 3]", "null"])
def test_non_dict_json_line_is_skipped(tmp_path: Path, payload: str) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_shard(
        root,
        "test-host",
        [payload, json.dumps(_record(goal_id="survives"))],
    )

    result = read_and_collapse(root)

    assert [row.goal_id for row in result.rows] == ["survives"]


def test_legacy_row_goal_id_unification_matches_writer_formula(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    legacy_record = _record()
    del legacy_record["goal_id"]
    _write_shard(root, "test-host", [json.dumps(legacy_record)])

    result = read_and_collapse(root, default_repo="test-org/test-repo")

    expected_id = _goal_id(
        legacy_record["repo"],
        legacy_record["coordinator_root_path"],
        legacy_record["period"],
        legacy_record["period_value"],
        legacy_record["text"],
    )
    assert len(result.rows) == 1
    assert result.rows[0].goal_id == expected_id


def test_legacy_row_missing_repo_key_falls_back_to_default_repo(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    legacy_record = _record()
    del legacy_record["goal_id"]
    del legacy_record["repo"]
    _write_shard(root, "test-host", [json.dumps(legacy_record)])

    result = read_and_collapse(root, default_repo="fallback-org/fallback-repo")

    expected_id = _goal_id(
        "fallback-org/fallback-repo",
        legacy_record["coordinator_root_path"],
        legacy_record["period"],
        legacy_record["period_value"],
        legacy_record["text"],
    )
    assert len(result.rows) == 1
    assert result.rows[0].goal_id == expected_id


def test_legacy_row_missing_coordinator_root_path_key_falls_back_to_dot(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    legacy_record = _record()
    del legacy_record["goal_id"]
    del legacy_record["coordinator_root_path"]
    _write_shard(root, "test-host", [json.dumps(legacy_record)])

    result = read_and_collapse(root, default_repo="test-org/test-repo")

    expected_id = _goal_id(
        legacy_record["repo"],
        ".",
        legacy_record["period"],
        legacy_record["period_value"],
        legacy_record["text"],
    )
    assert len(result.rows) == 1
    assert result.rows[0].goal_id == expected_id


def test_legacy_and_goal_id_bearing_row_collapse_as_supersession(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    legacy_record = _record(declared_at="2026-06-24T09:00:00Z")
    del legacy_record["goal_id"]
    computed_id = _goal_id(
        legacy_record["repo"],
        legacy_record["coordinator_root_path"],
        legacy_record["period"],
        legacy_record["period_value"],
        legacy_record["text"],
    )
    later_record = _record(
        goal_id=computed_id,
        text=legacy_record["text"],
        declared_at="2026-06-24T10:00:00Z",
        status="closed",
    )
    _write_shard(root, "test-host", [json.dumps(legacy_record), json.dumps(later_record)])

    result = read_and_collapse(root, default_repo="test-org/test-repo")

    assert len(result.rows) == 1
    assert result.rows[0].goal_id == computed_id
    assert result.rows[0].record["status"] == "closed"


def test_same_declared_at_tie_resolves_to_first_shard_in_sorted_order(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    tied_at = "2026-06-24T09:00:00Z"
    _write_shard(
        root,
        "a-machine",
        [json.dumps(_record(declared_at=tied_at, status="from-a"))],
    )
    _write_shard(
        root,
        "b-machine",
        [json.dumps(_record(declared_at=tied_at, status="from-b"))],
    )

    result = read_and_collapse(root)

    assert len(result.rows) == 1
    assert result.rows[0].record["status"] == "from-a"
    assert result.rows[0].shard_path.name == "goals-log.a-machine.jsonl"


def test_strict_declared_at_tiebreak_prefers_later_when_not_tied(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_shard(
        root,
        "a-machine",
        [json.dumps(_record(declared_at="2026-06-24T09:00:00Z", status="earlier"))],
    )
    _write_shard(
        root,
        "b-machine",
        [json.dumps(_record(declared_at="2026-06-24T10:00:00Z", status="later"))],
    )

    result = read_and_collapse(root)

    assert len(result.rows) == 1
    assert result.rows[0].record["status"] == "later"


def test_provenance_shard_path_matches_surviving_record(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    shard = _write_shard(root, "test-host", [json.dumps(_record())])

    result = read_and_collapse(root)

    assert len(result.rows) == 1
    assert result.rows[0].shard_path == shard
