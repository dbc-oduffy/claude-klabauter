"""test_leg_freshness: stamp, read and is_stale judge a leg measured on the work, not an older one."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import test_leg_freshness as tlf
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

RUN_ID = "run-fresh"
HEAD = "a" * 40
OTHER = "b" * 40


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "refs" / "heads" / "main").write_text(HEAD + "\n", encoding="utf-8")
    return root


def _leg(root: Path, **fields) -> Path:
    path = measured_rev_record_path(root, RUN_ID).parent / "test-leg.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def _own(root: Path, rel: str = "src/a.py", age_s: float = 0.0) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    stamp = datetime.now(timezone.utc).timestamp() + age_s
    os.utime(path, (stamp, stamp))


def test_stamp_adds_both_keys_and_keeps_the_rest():
    out = tlf.stamp({"status": "pass"}, measured_at="2026-11-24T20:00:00+00:00", head_sha=HEAD)
    assert out == {"status": "pass", "measured_at": "2026-11-24T20:00:00+00:00", "head_sha": HEAD}
    assert tlf.stamp({}, measured_at="t", head_sha=None)["head_sha"] is None


def test_started_stamp_reads_head_in_process_for_none_and_head(repo):
    for rev in (None, "HEAD"):
        at, sha = tlf.started_stamp(repo, rev)
        assert sha == HEAD
        assert datetime.fromisoformat(at).utcoffset() == timedelta(0)


def test_started_stamp_keeps_a_resolved_commit_not_current_head(repo):
    assert tlf.started_stamp(repo, OTHER)[1] == OTHER


def test_leg_stamp_reads_strings_and_nulls_the_rest():
    assert tlf.leg_stamp({"measured_at": "t", "head_sha": HEAD}) == {"measured_at": "t", "head_sha": HEAD}
    assert tlf.leg_stamp({"measured_at": 5, "head_sha": ""}) == {"measured_at": None, "head_sha": None}
    assert tlf.leg_stamp(None) == {"measured_at": None, "head_sha": None}


def test_fresh_leg_is_not_stale(repo):
    _own(repo, age_s=-60)
    _leg(repo, measured_at=_iso(0), head_sha=HEAD)
    assert tlf.is_stale(repo, RUN_ID, {"own_diff_paths": ["src/a.py"]}) is None


def test_no_leg_is_stale(repo):
    assert "no test-leg.json" in tlf.is_stale(repo, RUN_ID, None)


@pytest.mark.parametrize("fields", [
    {"status": "pass"},
    {"measured_at": "2026-11-24T20:00:00+00:00"},
    {"head_sha": HEAD},
    {"measured_at": "2026-11-24T20:00:00+00:00", "head_sha": None},
])
def test_unstamped_or_half_stamped_leg_is_stale(repo, fields):
    _leg(repo, **fields)
    assert "no measured_at/head_sha stamp" in tlf.is_stale(repo, RUN_ID, None)


def test_head_mismatch_is_stale(repo):
    _leg(repo, measured_at=_iso(0), head_sha=OTHER)
    reason = tlf.is_stale(repo, RUN_ID, None)
    assert reason and OTHER[:12] in reason and HEAD[:12] in reason


def test_leg_older_than_an_own_diff_write_is_stale(repo):
    _own(repo, age_s=+120)
    _leg(repo, measured_at=_iso(0), head_sha=HEAD)
    assert "src/a.py was written after" in tlf.is_stale(repo, RUN_ID, {"own_diff_paths": ["src/a.py"]})


def test_review_declared_paths_stand_in_for_own_diff_paths(repo):
    _own(repo, age_s=+120)
    _leg(repo, measured_at=_iso(0), head_sha=HEAD)
    assert tlf.is_stale(repo, RUN_ID, {"review_declared_paths": ["src/a.py"]})


def test_missing_escaping_and_absolute_own_diff_paths_do_not_stale_a_fresh_leg(repo, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("x", encoding="utf-8")
    future = datetime.now(timezone.utc).timestamp() + 600
    os.utime(outside, (future, future))
    _leg(repo, measured_at=_iso(0), head_sha=HEAD)
    manifest = {"own_diff_paths": ["gone.py", "../outside.py", str(outside)]}
    assert tlf.is_stale(repo, RUN_ID, manifest) is None


def test_unparseable_measured_at_is_stale(repo):
    _leg(repo, measured_at="yesterday", head_sha=HEAD)
    assert "not an ISO-8601" in tlf.is_stale(repo, RUN_ID, None)


def test_halt_stamp_reports_the_leg_and_current_head(repo):
    _leg(repo, measured_at=_iso(0), head_sha=OTHER)
    record_dir = measured_rev_record_path(repo, RUN_ID).parent
    out = tlf.halt_stamp(record_dir)
    assert out["head_sha"] == OTHER and out["current_head"] == HEAD and out["stale"] is True
    assert out["measured_at"]


def test_halt_stamp_fresh_leg_is_not_stale(repo):
    _leg(repo, measured_at=_iso(0), head_sha=HEAD)
    assert tlf.halt_stamp(measured_rev_record_path(repo, RUN_ID).parent)["stale"] is False


def test_halt_stamp_without_a_leg_is_null_and_stale(repo):
    record_dir = measured_rev_record_path(repo, RUN_ID).parent
    record_dir.mkdir(parents=True)
    assert tlf.halt_stamp(record_dir) == {"measured_at": None, "head_sha": None, "current_head": HEAD, "stale": True}
