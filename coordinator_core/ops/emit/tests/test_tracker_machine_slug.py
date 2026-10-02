"""tracker_machine_slug is injective, refuses a shared fallback, and leaves legacy shards readable."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core import tracker_store
from coordinator_core.ops.emit._slug import machine_slug, tracker_machine_slug


def test_hostnames_colliding_under_the_legacy_slug_stay_distinct():
    hosts = ["dev.box", "dev-box", "dev_box"]
    assert len({machine_slug(h) for h in hosts}) == 1
    assert len({tracker_machine_slug(h) for h in hosts}) == 3


def test_slug_is_sanitised_hostname_plus_stable_hex_suffix():
    slug = tracker_machine_slug("Dev.Box")
    assert re.fullmatch(r"dev-box-[0-9a-f]{6}", slug)
    assert slug == tracker_machine_slug("Dev.Box")


@pytest.mark.parametrize("raw", ["", "---", "..."])
def test_no_alphanumeric_hostname_fails_loud_instead_of_unknown(raw):
    with pytest.raises(ValueError):
        tracker_machine_slug(raw)


def test_tracker_store_mints_shards_under_the_new_slug(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("socket.gethostname", lambda: "dev.box")
    assert tracker_store.machine_slug() == tracker_machine_slug("dev.box")
    assert tracker_store.shard_path(tmp_path).name == f"events.{tracker_machine_slug('dev.box')}.jsonl"


def test_legacy_shard_stays_visible_alongside_the_new_one(tmp_path: Path):
    shard_dir = tmp_path / tracker_store.EVENTS_DIR_RELPATH
    shard_dir.mkdir(parents=True)
    (shard_dir / "events.dev-box.jsonl").write_text("{}\n", encoding="utf-8")
    (shard_dir / f"events.{tracker_machine_slug('dev.box')}.jsonl").write_text("{}\n", encoding="utf-8")
    assert tracker_store._all_machine_slugs(tmp_path) == {"dev-box", tracker_machine_slug("dev.box")}
