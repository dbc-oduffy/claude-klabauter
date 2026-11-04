"""The legacy token listing skips records older than one TTL: their tokens are expired, and reading
each one through its own bounded thread cost every landing and hook ~2s CPU over ~300 dead files."""
import os
import time

from coordinator_core.ops.dispatch_emit import run_token


def test_legacy_listing_keeps_only_records_inside_the_ttl(tmp_path):
    fresh, stale = tmp_path / "fresh.json", tmp_path / "stale.json"
    fresh.write_text("{}", encoding="utf-8")
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - run_token.TTL_SECONDS - 60
    os.utime(stale, (old, old))
    (tmp_path / "note.txt").write_text("", encoding="utf-8")

    assert run_token._bounded_glob(tmp_path, 1000.0) == [fresh]


def test_missing_legacy_dir_lists_nothing(tmp_path):
    assert run_token._bounded_glob(tmp_path / "absent", 1000.0) == []


def test_reap_deletes_expired_files_and_keeps_fresh_ones(tmp_path):
    fresh, stale = tmp_path / "fresh.json", tmp_path / "stale.json"
    fresh.write_text("{}", encoding="utf-8")
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - run_token.TTL_SECONDS - 60
    os.utime(stale, (old, old))

    assert run_token._bounded_glob(tmp_path, 1000.0, reap=True) == [fresh]
    assert not stale.exists() and fresh.exists()


def test_without_reap_an_expired_file_stays(tmp_path):
    stale = tmp_path / "stale.json"
    stale.write_text("{}", encoding="utf-8")
    old = time.time() - run_token.TTL_SECONDS - 60
    os.utime(stale, (old, old))

    assert run_token._bounded_glob(tmp_path, 1000.0) == []
    assert stale.exists()
