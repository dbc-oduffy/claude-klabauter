"""land_resume: one entry per run, claim by rename, bounded attempts and age."""

from __future__ import annotations

import os
import stat
import sys
import time

import pytest

from coordinator_core.ops.dispatch_emit import land_resume as lr
from coordinator_core.warm import breadcrumb


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    return tmp_path / "engine"


PARAMS = {"root": "R", "mode": "product"}


def test_queue_dir_under_runtime_base(root, tmp_path):
    assert str(lr.queue_dir(root)).startswith(str(tmp_path / "rt"))


def test_enqueue_creates_one_entry_with_fields(root):
    assert lr.enqueue(root, "run-1", PARAMS, "land_run.commit") is True
    entry = lr.load(root, "run-1")
    assert entry["run_id"] == "run-1"
    assert entry["params"] == PARAMS
    assert entry["attempts"] == 1
    assert entry["last_leg"] == "land_run.commit"
    assert entry["repo_root"] == "R"
    assert "enqueued_at" in entry


def test_second_enqueue_bumps_not_duplicates_or_resets(root):
    lr.enqueue(root, "run-1", PARAMS, "land_run.integrity")
    first = lr.load(root, "run-1")["enqueued_at"]
    assert lr.enqueue(root, "run-1", PARAMS, "land_run.commit") is True
    entry = lr.load(root, "run-1")
    assert entry["attempts"] == 2
    assert entry["last_leg"] == "land_run.commit"
    assert entry["enqueued_at"] == first
    names = [n for n in os.listdir(lr.queue_dir(root)) if n.endswith(lr.PENDING_SUFFIX)]
    assert names == ["run-1.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="posix mode bits")
def test_entry_is_0600(root):
    lr.enqueue(root, "run-1", PARAMS, "x")
    mode = stat.S_IMODE(os.stat(lr.queue_dir(root) / "run-1.json").st_mode)
    assert mode == 0o600


def test_unsafe_run_id_refused(root):
    assert lr.enqueue(root, "../x", PARAMS, "x") is False
    assert lr.enqueue(root, "", PARAMS, "x") is False


def test_empty_queue_next_due_is_none(root):
    assert lr.next_due(root, time.time()) is None


def test_next_due_claims_by_rename_and_second_drainer_gets_none(root):
    lr.enqueue(root, "run-1", PARAMS, "x")
    path = lr.next_due(root, time.time())
    assert path is not None and path.name == "run-1.claimed"
    assert not (lr.queue_dir(root) / "run-1.json").exists()
    assert lr.next_due(root, time.time()) is None


def test_finish_closes_and_blocks_requeue(root):
    lr.enqueue(root, "run-1", PARAMS, "x")
    lr.next_due(root, time.time())
    assert lr.finish(root, "run-1", lr.OUTCOME_LANDED) is True
    assert (lr.queue_dir(root) / "run-1.done").exists()
    assert lr.load(root, "run-1") is None
    assert lr.enqueue(root, "run-1", PARAMS, "x") is False
    assert lr.next_due(root, time.time()) is None


def test_enqueue_during_claim_requeues_on_finish(root):
    lr.enqueue(root, "run-1", PARAMS, "x")
    lr.next_due(root, time.time())
    assert lr.enqueue(root, "run-1", PARAMS, "land_run.commit") is True
    assert lr.finish(root, "run-1", "deadline") is True
    entry = lr.load(root, "run-1")
    assert entry["attempts"] == 2 and "requeued" not in entry
    assert (lr.queue_dir(root) / "run-1.json").exists()


def test_bump_racing_a_claim_leaves_one_live_entry(root, monkeypatch):
    lr.enqueue(root, "run-1", PARAMS, "x")
    real_write = lr._write
    raced = []

    def claim_then_write(path, entry):
        if not raced and path.name == "run-1.json":
            raced.append(lr.next_due(root, time.time()))
        real_write(path, entry)

    monkeypatch.setattr(lr, "_write", claim_then_write)
    assert lr.enqueue(root, "run-1", PARAMS, "land_run.commit") is True
    monkeypatch.setattr(lr, "_write", real_write)
    assert raced and raced[0] is not None
    directory = lr.queue_dir(root)
    assert not (directory / "run-1.json").exists()
    claimed = lr.read_entry(directory / "run-1.claimed")
    assert claimed["attempts"] == 2 and claimed["requeued"] is True
    assert lr.finish(root, "run-1", "deadline") is True
    assert lr.load(root, "run-1")["attempts"] == 2


def test_stale_claim_of_dead_holder_is_reclaimed(root, monkeypatch):
    lr.enqueue(root, "run-1", PARAMS, "x")
    claimed = lr.next_due(root, time.time())
    old = time.time() - lr.CLAIM_STALE_SECS - 10
    os.utime(claimed, (old, old))
    monkeypatch.setattr(lr.record, "pid_state", lambda pid: False)
    again = lr.next_due(root, time.time())
    assert again is not None and again.name == "run-1.claimed"


def test_stale_claim_of_live_or_doubtful_holder_is_kept(root, monkeypatch):
    lr.enqueue(root, "run-1", PARAMS, "x")
    claimed = lr.next_due(root, time.time())
    old = time.time() - lr.CLAIM_STALE_SECS - 10
    os.utime(claimed, (old, old))
    for state in (True, None):
        monkeypatch.setattr(lr.record, "pid_state", lambda pid, s=state: s)
        assert lr.next_due(root, time.time()) is None


def test_fresh_claim_is_not_reclaimed(root, monkeypatch):
    lr.enqueue(root, "run-1", PARAMS, "x")
    lr.next_due(root, time.time())
    monkeypatch.setattr(lr.record, "pid_state", lambda pid: False)
    assert lr.next_due(root, time.time()) is None


def test_expired_by_attempts_and_age():
    now = 10_000.0
    assert lr.expired({"attempts": lr.MAX_ATTEMPTS, "enqueued_at": now}, now) is False
    assert lr.expired({"attempts": lr.MAX_ATTEMPTS + 1, "enqueued_at": now}, now) is True
    assert lr.expired({"attempts": 1, "enqueued_at": now - lr.MAX_AGE_SECS + 1}, now) is False
    assert lr.expired({"attempts": 1, "enqueued_at": now - lr.MAX_AGE_SECS - 1}, now) is True


def test_next_due_finishes_expired_entry_and_skips_it(root):
    lr.enqueue(root, "old", PARAMS, "x")
    lr.enqueue(root, "fresh", PARAMS, "x")
    assert lr.next_due(root, time.time() + lr.MAX_AGE_SECS + 5) is None
    assert (lr.queue_dir(root) / "old.done").exists()
    assert (lr.queue_dir(root) / "fresh.done").exists()


def test_log_line_per_state_change(root):
    lr.enqueue(root, "run-1", PARAMS, "x")
    lr.enqueue(root, "run-1", PARAMS, "y")
    lr.next_due(root, time.time())
    lr.finish(root, "run-1", lr.OUTCOME_LANDED)
    lines = (lr.queue_dir(root) / lr.LOG_FILENAME).read_text().splitlines()
    states = [ln.split("state=")[1].split()[0] for ln in lines]
    assert states == ["enqueued", "bumped", "claimed", "finished"]
