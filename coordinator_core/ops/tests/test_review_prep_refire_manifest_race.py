"""A stale-record refire's review prep waits on the run's recompose claim for a complete manifest."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.tests.test_review_prep import _write, repo  # noqa: F401

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-race"
DECLARED = ["a/one.py"]


def _atomic_manifest(rd: Path, declared) -> None:
    tmp = rd / "manifest.json.tmp"
    tmp.write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")
    os.replace(tmp, rd / "manifest.json")


def _finish_later(rd: Path, delay_s: float, declared, *, release: bool = True) -> threading.Thread:
    def _run() -> None:
        time.sleep(delay_s)
        _atomic_manifest(rd, declared)
        if release:
            hrr.release_recompose(rd)

    t = threading.Thread(target=_run)
    t.start()
    return t


def _paths(repo: Path) -> list[str]:
    return rp.declared_paths_and_held(repo, RUN, own_session="sess-own")[0]


def test_absent_manifest_under_a_held_claim_is_awaited(repo) -> None:
    _write(repo, DECLARED, 3)
    rd = record_dir(repo, RUN)
    assert hrr.claim_recompose(rd, time.time(), hrr.RECOMPOSE_TTL_S)
    t = _finish_later(rd, 0.1, DECLARED)
    try:
        assert _paths(repo) == DECLARED
    finally:
        t.join()


def test_partial_manifest_under_a_held_claim_is_awaited(repo) -> None:
    _write(repo, DECLARED, 3)
    rd = record_dir(repo, RUN)
    assert hrr.claim_recompose(rd, time.time(), hrr.RECOMPOSE_TTL_S)
    (rd / "manifest.json").write_text('{"review_declared_', encoding="utf-8")
    t = _finish_later(rd, 0.1, DECLARED)
    try:
        assert _paths(repo) == DECLARED
    finally:
        t.join()


def test_a_manifest_landing_within_one_poll_of_the_claim_is_awaited(repo) -> None:
    _write(repo, DECLARED, 3)
    rd = record_dir(repo, RUN)
    assert hrr.claim_recompose(rd, time.time(), hrr.RECOMPOSE_TTL_S)
    t = _finish_later(rd, 0.05, DECLARED)
    try:
        assert _paths(repo) == DECLARED
    finally:
        t.join()


def test_no_claim_and_no_manifest_raises_promptly(repo) -> None:
    record_dir(repo, RUN).mkdir(parents=True)
    started = time.monotonic()
    with pytest.raises(ValueError, match="no readable manifest"):
        _paths(repo)
    assert time.monotonic() - started < rp.MANIFEST_WAIT_S / 2


def test_a_stale_claim_is_not_waited_on(repo) -> None:
    rd = record_dir(repo, RUN)
    rd.mkdir(parents=True)
    assert hrr.claim_recompose(rd, time.time() - hrr.RECOMPOSE_TTL_S - 1, hrr.RECOMPOSE_TTL_S)
    started = time.monotonic()
    with pytest.raises(ValueError, match="no readable manifest"):
        _paths(repo)
    assert time.monotonic() - started < rp.MANIFEST_WAIT_S / 2


def test_a_claim_that_never_produces_a_manifest_waits_only_the_cap(repo) -> None:
    rd = record_dir(repo, RUN)
    assert hrr.claim_recompose(rd, time.time(), hrr.RECOMPOSE_TTL_S)
    started = time.monotonic()
    with pytest.raises(ValueError, match="no readable manifest"):
        _paths(repo)
    elapsed = time.monotonic() - started
    assert rp.MANIFEST_WAIT_S <= elapsed < rp.MANIFEST_WAIT_S + 0.5


def test_land_blob_rewrites_a_same_size_corrupt_blob_and_keeps_a_correct_one(tmp_path: Path) -> None:
    blob = tmp_path / "digest"
    blob.write_bytes(b"abcd")
    assert rp._land_blob(blob, b"wxyz") is True
    assert blob.read_bytes() == b"wxyz"
    assert rp._land_blob(blob, b"wxyz") is False
